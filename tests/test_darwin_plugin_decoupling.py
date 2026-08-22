"""
El desacoplamiento de los plugins de Darwin, con la misma semántica que las piezas de
almacenamiento: un extra por unidad de decisión del consumidor, y una frontera verificada.

Lo que este archivo fija:

- **El núcleo no conoce a ningún plugin.** Ni por import ni por nombre. La tentación acá es
  enumerar los propósitos de los plugins en el `Literal` de `VerificationPurpose` —`verification`
  es la tabla que los plugins reusan en vez de aportar una propia— y eso metía dos nombres de
  plugin en el dominio del núcleo.
- **Ningún plugin conoce a otro.** Comparten `plugins/storage.py`, que es del núcleo, y nada más.
  Sin esto, "instalá sólo `[darwin-passkey]`" es una promesa que se rompe en el primer import.
- **Cada plugin tiene su extra, y el extra arrastra el núcleo.** Cuatro de los seis no suman
  paquetes, así que la autorreferencia es lo único que hace que
  `pip install 'hexcore[darwin-two-factor]'` instale algo que funcione.
- **La correspondencia es total en las dos direcciones**: un plugin sin extra es un plugin que
  nadie encuentra, y un extra sin plugin es un comando de instalación que miente.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("joserfc")
pytest.importorskip("argon2")

RAIZ = Path(__file__).resolve().parent.parent
PLUGINS_DIR = RAIZ / "hexcore" / "darwin" / "plugins"

#: Los seis plugins, con el extra que les corresponde. El guión bajo del paquete es un guión
#: medio en el extra porque un nombre de extra se normaliza así (PEP 503) y `pip` acepta las dos
#: formas — pero la que se documenta tiene que ser la canónica.
PLUGINS: tuple[tuple[str, str], ...] = (
    ("magic_link", "darwin-magic-link"),
    ("two_factor", "darwin-two-factor"),
    ("oauth", "darwin-oauth"),
    ("impersonate", "darwin-impersonate"),
    ("passkey", "darwin-passkey"),
    ("organization", "darwin-organization"),
)

#: Los paquetes del núcleo, que son los que no pueden nombrar un plugin.
NUCLEO: tuple[str, ...] = (
    "hexcore/darwin/domain",
    "hexcore/darwin/application",
    "hexcore/darwin/infrastructure",
)


def _extras() -> dict[str, list[str]]:
    datos = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))
    return datos["project"]["optional-dependencies"]


def _fuentes(directorio: Path) -> list[Path]:
    return [p for p in directorio.rglob("*.py") if "__pycache__" not in p.parts]


def _imports_de(archivo: Path) -> list[str]:
    """
    Los módulos que el archivo importa, del AST.

    Del AST y no escaneando líneas, por dos razones que apuntan en direcciones opuestas y las dos
    importan:

    - **Alcanza más.** Un `importlib.import_module` diferido dentro de un método no se ejecuta al
      importar el módulo, así que ningún test de humo lo encuentra — pero el paquete tiene que
      estar instalado para que ese método corra. El AST lo ve igual, esté donde esté.
    - **Alcanza menos, y hace falta.** Los docstrings de esta casa llevan bloques ``Uso::`` con
      imports de ejemplo, y un escaneo de líneas los cuenta como imports reales. Eso daba un
      falso positivo en `plugins/storage.py`, cuyo docstring muestra cómo importarlo.
    """
    arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    modulos: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            modulos.extend(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module is not None:
            modulos.append(nodo.module)
            # `from paquete import submodulo` también es un import del submódulo, y es la forma
            # que se colaba cuando la separación del almacenamiento reescribió las rutas.
            modulos.extend(f"{nodo.module}.{alias.name}" for alias in nodo.names)
    return modulos


def _pertenece_a(modulo: str, plugin: str) -> bool:
    """`True` si `modulo` es el paquete de ese plugin o algo adentro."""
    raiz = f"hexcore.darwin.plugins.{plugin}"
    return modulo == raiz or modulo.startswith(raiz + ".")


def _es_de_un_plugin(modulo: str) -> bool:
    """
    `True` si el módulo es de alguno de los seis plugins.

    Se compara contra la lista de plugins y no contra el prefijo `hexcore.darwin.plugins.`, así
    que `plugins.storage` —que es del núcleo aunque viva ahí— no cuenta, y no hace falta una
    exclusión por nombre que dejaría pasar cualquier otra cosa.
    """
    return any(_pertenece_a(modulo, plugin) for plugin, _ in PLUGINS)


def _bloqueando_plugins(
    cuerpo: str, *, excepto: str | None = None
) -> subprocess.CompletedProcess[str]:
    """
    Corre `cuerpo` en un subproceso donde **importar un plugin de Darwin explota**.

    Es la técnica de `test_optional_dependencies.py` y de `test_darwin_storage_selection.py`, con
    una diferencia: el `MockFinder` de aquéllos filtra por el primer componente del nombre
    (`sqlalchemy`, `beanie`), y acá todos los nombres empiezan con `hexcore`. Así que filtra por
    prefijo del nombre punteado completo.

    Se bloquea en vez de mirar `sys.modules` por la misma razón de siempre: preguntar quién está
    cargado no distingue "lo importó el núcleo" de "ya estaba". Bloquear responde la pregunta que
    importa: ¿el núcleo arranca sin los plugins instalados?
    """
    prohibidos = [p for p, _ in PLUGINS if p != excepto]
    codigo = (
        "import sys\n"
        f"PROHIBIDOS = {prohibidos!r}\n"
        "class MockFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, fullname, path, target=None):\n"
        "        for p in PROHIBIDOS:\n"
        "            pre = 'hexcore.darwin.plugins.' + p\n"
        "            if fullname == pre or fullname.startswith(pre + '.'):\n"
        "                raise ImportError('bloqueado: ' + fullname)\n"
        "        return None\n"
        "sys.meta_path.insert(0, MockFinder)\n"
    ) + cuerpo
    return subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, cwd=RAIZ
    )


# ── El núcleo no conoce a ningún plugin ───────────────────────────────────────
class TestElNucleoNoConoceLosPlugins:
    def test_ningun_modulo_del_nucleo_importa_un_plugin(self) -> None:
        """
        Ni con `from`, ni con `import`, ni dentro de una función.

        Se lee el texto y no `sys.modules` porque un import diferido dentro de un método no se
        ejecuta al importar el módulo y ningún test de humo lo encontraría — pero el paquete
        tiene que estar instalado para que ese método corra.
        """
        culpables: list[str] = []
        for paquete in NUCLEO:
            for archivo in _fuentes(RAIZ / paquete):
                for modulo in _imports_de(archivo):
                    if _es_de_un_plugin(modulo):
                        culpables.append(f"{archivo.relative_to(RAIZ)}: {modulo}")
        assert culpables == [], (
            "El núcleo importa un plugin. La dirección de la dependencia es al revés: el plugin "
            "importa el núcleo.\n" + "\n".join(culpables)
        )

    def test_storage_no_importa_ningun_plugin(self) -> None:
        """
        El resolvedor está bajo `plugins/` pero es del núcleo, así que tampoco puede nombrarlos.

        `_es_de_un_plugin` lo deja pasar a propósito —es del núcleo— y este test es lo que
        impide que esa concesión se convierta en un agujero: sin él, mover un import de plugin a
        `storage.py` lo volvería invisible.
        """
        archivo = PLUGINS_DIR / "storage.py"
        culpables = [m for m in _imports_de(archivo) if _es_de_un_plugin(m)]
        assert culpables == [], "\n".join(culpables)

    def test_el_nucleo_importa_con_los_seis_plugins_bloqueados(self) -> None:
        resultado = _bloqueando_plugins(
            "import hexcore.darwin\n"
            "from hexcore.darwin.application.services import IdentityService\n"
            "from hexcore.darwin.application.container import IdentityContainer\n"
            "from hexcore.darwin.domain.ports import AbstractVerificationRepository\n"
            # El camino que junta los esquemas: pasa por `plugins.storage`, así que si ese
            # módulo arrastrara un plugin, esto explotaría con los seis bloqueados.
            "from hexcore.darwin.infrastructure.orms.sqlalchemy.schema import (\n"
            "    ensure_identity_schema_loaded,\n"
            ")\n"
            "assert ensure_identity_schema_loaded() != []\n"
            "print('ok')\n"
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok" in resultado.stdout

    def test_verification_purpose_es_abierto(self) -> None:
        """
        El `purpose` de `verification` es `str`, no un `Literal` con los nombres de los plugins.

        Es el acoplamiento que quedaba después de separar el almacenamiento: `verification` es la
        tabla que los plugins reusan en vez de aportar una propia, así que enumerar los propósitos
        cerraba el tipo alrededor de `"magic_link"` y `"two_factor"` — dos plugins que el núcleo
        puede no tener instalados.
        """
        from hexcore.darwin.domain.value_objects import VerificationPurpose

        assert VerificationPurpose is str

    def test_el_purpose_del_comando_publico_sigue_cerrado(self) -> None:
        """
        Abrir el tipo de la columna **no** abrió la superficie HTTP.

        Si `IssueVerificationCode.purpose` aceptara cualquier string, se podía pedir un código con
        `purpose="password_reset"` por el endpoint de verificar mail y canjearlo después en el
        flujo de reset — que es exactamente el cruce de flujos que el `purpose` existe para
        impedir.
        """
        import typing as t

        from pydantic import ValidationError

        from hexcore.darwin.application.commands import IssueVerificationCode
        from hexcore.darwin.domain.value_objects import CoreVerificationPurpose

        assert set(t.get_args(CoreVerificationPurpose)) == {
            "email_verification",
            "password_reset",
            "otp",
        }
        for propio in t.get_args(CoreVerificationPurpose):
            comando = IssueVerificationCode(email="a@b.com", purpose=propio)
            assert comando.purpose == propio
        for ajeno in ("magic_link", "two_factor", "cualquier-cosa"):
            with pytest.raises(ValidationError):
                IssueVerificationCode(email="a@b.com", purpose=ajeno)  # type: ignore[arg-type]

    def test_el_purpose_de_cada_plugin_lo_declara_el_plugin(self) -> None:
        """El núcleo transporta el valor; el nombre es del plugin."""
        from hexcore.darwin.plugins.magic_link import MAGIC_LINK_PURPOSE
        from hexcore.darwin.plugins.two_factor.service import TWO_FACTOR_PURPOSE

        assert MAGIC_LINK_PURPOSE == "magic_link"
        assert TWO_FACTOR_PURPOSE == "two_factor"


# ── Ningún plugin conoce a otro ───────────────────────────────────────────────
class TestLosPluginsNoSeConocenEntreSi:
    @pytest.mark.parametrize("plugin", [p for p, _ in PLUGINS])
    def test_no_importa_otro_plugin(self, plugin: str) -> None:
        ajenos = [p for p, _ in PLUGINS if p != plugin]
        culpables: list[str] = []
        for archivo in _fuentes(PLUGINS_DIR / plugin):
            for modulo in _imports_de(archivo):
                if any(_pertenece_a(modulo, otro) for otro in ajenos):
                    culpables.append(f"{archivo.relative_to(RAIZ)}: {modulo}")
        assert culpables == [], "\n".join(culpables)

    @pytest.mark.parametrize("plugin", [p for p, _ in PLUGINS])
    def test_importa_con_los_otros_cinco_bloqueados(self, plugin: str) -> None:
        """
        La prueba de que el extra de un plugin es instalable solo.

        Se bloquean los otros cinco, no se mira `sys.modules`: ver `_bloqueando_plugins`.
        """
        resultado = _bloqueando_plugins(
            f"import hexcore.darwin.plugins.{plugin}\nprint('ok')\n",
            excepto=plugin,
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok" in resultado.stdout


# ── Un extra por plugin, y el extra arrastra el núcleo ────────────────────────
class TestLosExtrasDeLosPlugins:
    @pytest.mark.parametrize(("plugin", "extra"), PLUGINS)
    def test_el_extra_existe(self, plugin: str, extra: str) -> None:
        assert extra in _extras(), (
            f"El plugin `{plugin}` no tiene extra. Un plugin ausente de "
            f"[project.optional-dependencies] es un plugin que nadie encuentra."
        )

    @pytest.mark.parametrize(("plugin", "extra"), PLUGINS)
    def test_el_extra_arrastra_el_nucleo(self, plugin: str, extra: str) -> None:
        """
        `hexcore[darwin]` va en cada extra de plugin, y no es cosmético.

        Cuatro de los seis plugins no suman paquetes. Sin la autorreferencia,
        `pip install 'hexcore[darwin-two-factor]'` resolvía a un extra vacío: instalaba el
        paquete sin `joserfc` ni `argon2`, y el primer import del plugin explotaba.
        """
        requisitos = _extras()[extra]
        assert "hexcore[darwin]" in requisitos, (
            f"`{extra}` no arrastra `hexcore[darwin]`, así que instalarlo solo deja el plugin "
            f"sin el núcleo que necesita."
        )

    @pytest.mark.parametrize(("plugin", "extra"), PLUGINS)
    def test_el_plugin_existe(self, plugin: str, extra: str) -> None:
        assert (PLUGINS_DIR / plugin / "__init__.py").is_file()

    def test_no_hay_extra_de_darwin_sin_su_contraparte(self) -> None:
        """
        Un `[darwin-*]` que no es ni un plugin ni un backend es un comando que miente.

        Es la dirección que un test por plugin no cubre: agregar el extra y borrar el paquete deja
        el nombre publicado y el import roto.
        """
        de_almacenamiento = {"darwin", "darwin-sqlalchemy", "darwin-beanie"}
        de_plugins = {e for _, e in PLUGINS}
        publicados = {e for e in _extras() if e == "darwin" or e.startswith("darwin-")}
        assert publicados == de_almacenamiento | de_plugins, (
            "Los extras `[darwin-*]` publicados no coinciden con los que existen. "
            f"Sobran: {sorted(publicados - de_almacenamiento - de_plugins)}. "
            f"Faltan: {sorted((de_almacenamiento | de_plugins) - publicados)}."
        )

    def test_todo_paquete_bajo_plugins_esta_en_la_tabla(self) -> None:
        """Un plugin nuevo no puede entrar sin extra: este test lo encuentra."""
        declarados = {p for p, _ in PLUGINS}
        en_disco = {
            d.name
            for d in PLUGINS_DIR.iterdir()
            if d.is_dir() and (d / "__init__.py").is_file() and d.name != "__pycache__"
        }
        assert en_disco == declarados, (
            f"Plugins en disco que la tabla no lista: {sorted(en_disco - declarados)}. "
            f"Listados que no existen: {sorted(declarados - en_disco)}."
        )

    def test_el_extra_all_incluye_las_dependencias_de_todos(self) -> None:
        """
        `[all]` es lo que corre en CI, así que si le falta algo, algo no se testea.

        Se compara sólo lo que son paquetes: la autorreferencia `hexcore[darwin]` no va en `[all]`
        porque `[all]` **es** ese conjunto expandido a mano.
        """
        extras = _extras()
        todos = set(extras["all"])
        piezas = ["darwin", "darwin-sqlalchemy", "darwin-beanie"]
        piezas += [e for _, e in PLUGINS]
        for extra in piezas:
            for requisito in extras[extra]:
                if requisito.startswith("hexcore["):
                    continue
                assert requisito in todos, (
                    f"`{requisito}` está en `[{extra}]` pero no en `[all]`, así que CI corre sin él."
                )
