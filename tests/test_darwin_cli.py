"""
Darwin Fase 8: la sub-app de Typer, `hexcore identity ...`.

Es el primer `add_typer` del repo, y trae un contrato frágil que ningún otro módulo tiene:
`hexcore/__init__.py` importa `hexcore.infrastructure.cli` **eagerly**, y ese módulo hace
`add_typer(identity_cli)`. O sea que todo lo que `hexcore/darwin/infrastructure/cli.py`
importe en el nivel superior se carga con cada `import hexcore`, en cualquier proceso, tenga o
no los extras.

Por eso el test central de este archivo es sobre los **imports** y no sobre la salida de los
comandos: un `from hexcore.darwin import ...` en el nivel superior de ese módulo rompería
`import hexcore` en un entorno sin `[darwin]`, y el síntoma aparecería en el arranque del
consumidor, no acá.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

from hexcore.darwin.infrastructure.cli import identity_cli  # noqa: E402
from hexcore.infrastructure.cli import app  # noqa: E402

RUNNER = CliRunner()
CLI_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "hexcore"
    / "darwin"
    / "infrastructure"
    / "cli.py"
)


# ── El contrato de imports ────────────────────────────────────────────────────
def test_el_modulo_solo_importa_typer_y_stdlib_arriba():
    """
    Se lee el AST y no se mira `sys.modules`: acá Darwin ya está importado por los otros
    tests, así que preguntarle al intérprete no distinguiría entre "lo importó este módulo" y
    "ya estaba". El AST responde exactamente la pregunta que importa.
    """
    arbol = ast.parse(CLI_PATH.read_text(encoding="utf-8"))

    raices: set[str] = set()
    for nodo in arbol.body:  # sólo el nivel superior; los de adentro de funciones están bien
        if isinstance(nodo, ast.Import):
            raices.update(a.name.split(".")[0] for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            raices.add(nodo.module.split(".")[0])

    permitidas = {"typer", "__future__"} | set(sys.stdlib_module_names)

    assert raices <= permitidas, (
        f"{sorted(raices - permitidas)} se importa en el nivel superior de "
        f"hexcore/darwin/infrastructure/cli.py. `hexcore/__init__.py` arrastra ese módulo "
        f"eager, así que eso rompe `import hexcore` en un proceso sin los extras. Movelo "
        f"adentro del comando que lo necesita."
    )


def test_importar_hexcore_no_arrastra_darwin():
    """
    En un subproceso limpio, porque en este intérprete Darwin ya está cargado por los otros
    tests y la pregunta no se podría hacer.
    """
    # Los tres módulos esperados son inevitables: importar `...infrastructure.cli` ejecuta los
    # `__init__` de sus paquetes padre. Los tres son fachadas perezosas, así que ninguno importa
    # un adaptador. Lo que este test prohíbe es que aparezca un cuarto —un `container`, un
    # `models`, un `services`— que sí arrastraría los extras.
    codigo = (
        "import sys, hexcore\n"
        "esperado = {'hexcore.darwin', 'hexcore.darwin.infrastructure', "
        "'hexcore.darwin.infrastructure.cli'}\n"
        "cargados = {m for m in sys.modules if m.startswith('hexcore.darwin')}\n"
        "assert cargados == esperado, sorted(cargados - esperado)\n"
        "print('ok')\n"
    )

    resultado = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True
    )

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert "ok" in resultado.stdout


# ── El cableado ───────────────────────────────────────────────────────────────
def test_identity_esta_registrado_en_el_cli_raiz():
    nombres = [grupo.name for grupo in app.registered_groups]

    assert "identity" in nombres


def test_la_ayuda_raiz_lista_identity():
    resultado = RUNNER.invoke(app, ["--help"])

    assert resultado.exit_code == 0
    assert "identity" in resultado.output


def test_los_comandos_estan_todos():
    resultado = RUNNER.invoke(identity_cli, ["--help"])

    assert resultado.exit_code == 0
    for comando in (
        "generate-secret",
        "generate-keys",
        "create-tables",
        "check-schema",
        "plugins",
    ):
        assert comando in resultado.output


# ── `generate-secret` ─────────────────────────────────────────────────────────
def test_generate_secret_saca_una_clave_usable():
    """
    Existe para que nadie ponga `"changeme"`: el secreto no tiene default a propósito, y el
    primer obstáculo de quien cablea Darwin es no saber cómo generar uno.
    """
    resultado = RUNNER.invoke(identity_cli, ["generate-secret"])

    assert resultado.exit_code == 0
    clave = resultado.output.strip()
    assert len(clave) >= 32, "una clave corta invalida el punto del comando"

    # Y es aceptable para la config, que exige un mínimo.
    from hexcore.darwin import IdentityConfig

    assert IdentityConfig(secret_key=clave).secret_key is not None


def test_dos_corridas_no_dan_la_misma_clave():
    """El chequeo de que sale de un CSPRNG y no de una constante."""
    una = RUNNER.invoke(identity_cli, ["generate-secret"]).output.strip()
    otra = RUNNER.invoke(identity_cli, ["generate-secret"]).output.strip()

    assert una != otra


# ── `generate-keys` ───────────────────────────────────────────────────────────
def test_generate_keys_saca_un_jwk():
    pytest.importorskip("joserfc")
    import json

    resultado = RUNNER.invoke(identity_cli, ["generate-keys", "--kid", "k1"])

    assert resultado.exit_code == 0
    clave = json.loads(resultado.stdout)
    assert clave["kid"] == "k1"
    assert clave["algorithm"] == "Ed25519"
    # Los JWK salen parseados, no como un string de JSON adentro de un JSON.
    assert clave["public_jwk"]["kty"] == "OKP"
    assert "d" not in clave["public_jwk"], "la pública no puede llevar el escalar privado"
    assert "d" in clave["private_jwk"]

    # Y la clave que sale es cargable: el comando no sirve si hay que retocar la salida.
    from hexcore.darwin import StaticKeyStore
    from hexcore.darwin.infrastructure.keys import SigningKey

    StaticKeyStore(
        [
            SigningKey(
                kid=clave["kid"],
                algorithm=clave["algorithm"],
                public_key=json.dumps(clave["public_jwk"]),
                private_key=json.dumps(clave["private_jwk"]),
            )
        ]
    )


# ── `check-schema` ────────────────────────────────────────────────────────────
def test_check_schema_pasa_con_las_tablas_cargadas():
    """
    Es el chequeo que evita la pérdida de datos más cara del módulo: una tabla que existe en la
    base y falta en `Base.metadata` hace que `alembic revision --autogenerate` le emita
    `op.drop_table`.
    """
    pytest.importorskip("sqlalchemy")
    import hexcore.darwin.infrastructure.models  # noqa: F401  # las registra

    resultado = RUNNER.invoke(identity_cli, ["check-schema"])

    assert resultado.exit_code == 0
    assert "Base.metadata" in resultado.output


# ── `plugins` ─────────────────────────────────────────────────────────────────
def test_plugins_imprime_el_orden_resuelto(tmp_path, monkeypatch):
    """
    El comando existe porque el orden es topológico y no el de registro: verlo impreso es la
    única forma de confirmar que un plugin corre donde uno cree.
    """
    modulo = tmp_path / "mis_plugins.py"
    modulo.write_text(
        "from hexcore.darwin import DarwinPlugin\n"
        "\n"
        "class A(DarwinPlugin):\n"
        "    name = 'a'\n"
        "    requires = ('b',)\n"
        "\n"
        "class B(DarwinPlugin):\n"
        "    name = 'b'\n"
        "\n"
        "PLUGINS = [A(), B()]\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    resultado = RUNNER.invoke(identity_cli, ["plugins", "mis_plugins"])

    assert resultado.exit_code == 0
    lineas = [linea for linea in resultado.output.splitlines() if linea.strip()]
    assert lineas[0].startswith("1. b"), resultado.output
    assert lineas[1].startswith("2. a"), resultado.output
    assert "requiere: b" in lineas[1]


def test_plugins_falla_si_el_modulo_no_declara_nada(tmp_path, monkeypatch):
    modulo = tmp_path / "vacio_de_plugins.py"
    modulo.write_text("X = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    resultado = RUNNER.invoke(identity_cli, ["plugins", "vacio_de_plugins"])

    assert resultado.exit_code == 1
    assert "no expone" in resultado.output


def test_plugins_propaga_el_error_de_validacion(tmp_path, monkeypatch):
    """
    Valida antes de imprimir: un listado ordenado de un cableado inválido daría la impresión
    de que está bien.
    """
    modulo = tmp_path / "plugins_en_ciclo.py"
    modulo.write_text(
        "from hexcore.darwin import DarwinPlugin\n"
        "\n"
        "class A(DarwinPlugin):\n"
        "    name = 'a'\n"
        "    requires = ('b',)\n"
        "\n"
        "class B(DarwinPlugin):\n"
        "    name = 'b'\n"
        "    requires = ('a',)\n"
        "\n"
        "PLUGINS = [A(), B()]\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    resultado = RUNNER.invoke(identity_cli, ["plugins", "plugins_en_ciclo"])

    assert resultado.exit_code != 0
    assert resultado.exception is not None
    assert "ciclo" in str(resultado.exception)
