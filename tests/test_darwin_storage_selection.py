"""
La separación de Darwin en tres piezas: `[darwin]`, `[darwin-sqlalchemy]`, `[darwin-beanie]`.

Lo que este archivo fija es **la frontera**, que es lo único que hace que la separación sea real y
no una convención de nombres:

- El núcleo —dominio, aplicación, tokens, transportes— **no importa ningún backend**. Ni en el
  nivel superior ni transitivamente.
- Cada backend expone los **cinco nombres neutros** que el contenedor busca. Un backend al que le
  falta uno falla recién cuando alguien usa ese repositorio, que puede ser meses después.
- La resolución es explícita primero y detectada después, y **con dos backends instalados se
  niega a elegir**. Elegir por una regla implícita hace que el backend dependa de qué más haya en
  el entorno, y el síntoma es una app que arranca contra una base vacía.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from hexcore.darwin.infrastructure.orms.selection import (  # noqa: E402
    BACKENDS,
    installed_backends,
    resolve_storage_backend,
)

def _sin_backends(cuerpo: str) -> subprocess.CompletedProcess[str]:
    """
    Corre `cuerpo` en un subproceso donde **los dos backends están bloqueados**.

    Es la técnica de `test_optional_dependencies.py`: un `MockFinder` en `sys.meta_path` que
    levanta `ImportError` para `sqlalchemy` y `beanie`. Se hace así y no mirando `sys.modules`
    porque `import hexcore` arrastra sqlalchemy por su cuenta —`hexcore/__init__.py` importa
    `BaseSQLAlchemyRepository` eager— así que preguntar quién está cargado no distingue entre
    "Darwin lo importó" y "ya estaba".

    Bloquearlos responde la pregunta que importa: ¿`[darwin]` es instalable sin ningún backend?
    """
    codigo = (
        "import sys\n"
        "class MockFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, fullname, path, target=None):\n"
        "        if fullname.split('.')[0] in ('sqlalchemy', 'beanie'):\n"
        "            raise ImportError('bloqueado: ' + fullname)\n"
        "        return None\n"
        "sys.meta_path.insert(0, MockFinder)\n"
        + cuerpo
    )
    return subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True
    )


def _bloqueando(paquete: str, cuerpo: str) -> subprocess.CompletedProcess[str]:
    """
    Corre `cuerpo` en un subproceso con **un** paquete bloqueado.

    La versión de un solo paquete de `_sin_backends`, para los tests de simetría: se bloquea el
    backend que no se está probando.
    """
    codigo = (
        "import sys\n"
        "class MockFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, fullname, path, target=None):\n"
        f"        if fullname.split('.')[0] == {paquete!r}:\n"
        "            raise ImportError('bloqueado: ' + fullname)\n"
        "        return None\n"
        "sys.meta_path.insert(0, MockFinder)\n"
        + cuerpo
    )
    return subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True
    )


#: Los cinco nombres que todo backend del núcleo tiene que exponer.
CONTRATO_NUCLEO = (
    "UserRepository",
    "SessionRepository",
    "AccountRepository",
    "VerificationRepository",
    "AuditSink",
)

#: Y los de cada plugin con almacenamiento.
CONTRATO_PLUGINS = {
    "two_factor": ("TwoFactorRepository",),
    "oauth": ("OAuthStateRepository",),
    "passkey": ("PasskeyRepository", "PasskeyChallengeRepository"),
    "organization": (
        "OrganizationRepository",
        "MemberRepository",
        "InvitationRepository",
    ),
}


# ── La resolución ─────────────────────────────────────────────────────────────
class TestResolucion:
    def test_explicito_gana(self):
        """Si el consumidor lo declaró, no se detecta nada."""
        assert resolve_storage_backend("sqlalchemy") == "sqlalchemy"

    def test_un_nombre_inventado_se_rechaza(self):
        with pytest.raises(ValueError, match="no existe"):
            resolve_storage_backend("postgres")

    def test_el_mensaje_nombra_los_backends_validos(self):
        with pytest.raises(ValueError) as excinfo:
            resolve_storage_backend("cassandra")

        mensaje = str(excinfo.value)
        assert "sqlalchemy" in mensaje and "beanie" in mensaje

    def test_los_dos_backends_instalados_se_niegan_a_elegir(self):
        """
        ⚠️ **La decisión que importa.** El entorno de dev tiene los dos, así que este test corre
        contra la condición real. Elegir por orden alfabético o por orden de import haría que el
        mismo `pyproject.toml` dé un backend distinto según qué más haya instalado.
        """
        if len(installed_backends()) < 2:
            pytest.skip("hace falta tener los dos extras para probar la ambigüedad")

        with pytest.raises(ValueError) as excinfo:
            resolve_storage_backend(None)

        mensaje = str(excinfo.value)
        assert "más de un backend" in mensaje
        assert "IdentityConfig(storage=" in mensaje, "la remediación tiene que ser copiable"
        assert "HEXCORE_DARWIN_STORAGE" in mensaje

    def test_installed_backends_no_importa_los_paquetes(self):
        """
        Con `find_spec` y no con un `try: import`: importar sqlalchemy para averiguar si está
        cuesta ~200 ms y lo deja cargado en un proceso que quizá no lo necesitaba.

        Se mide alrededor de **la llamada** y no alrededor del import del módulo: `import
        hexcore` ya arrastra sqlalchemy por su cuenta, así que la pregunta sólo tiene respuesta
        si se acota a lo que hace la función.
        """
        import sys as _sys

        from hexcore.darwin.infrastructure.orms.selection import BACKENDS

        # Se saca de `sys.modules` lo que la llamada podría cargar, para poder ver si lo vuelve
        # a poner. `beanie` alcanza: es el que no importa nada más del framework.
        rescatado = {
            nombre: _sys.modules.pop(nombre)
            for nombre in list(_sys.modules)
            if nombre == "beanie" or nombre.startswith("beanie.")
        }
        try:
            assert installed_backends()  # hay al menos uno en el entorno de dev
            assert "beanie" not in _sys.modules, (
                "`installed_backends` importó el paquete en vez de usar `find_spec`"
            )
        finally:
            _sys.modules.update(rescatado)

        assert set(BACKENDS) == {"sqlalchemy", "beanie"}

    def test_el_mapa_de_backends_apunta_a_paquetes_de_tercero(self):
        """
        Se chequea el paquete de tercero y no el módulo de Darwin: `...orms.beanie` viene en la
        wheel siempre, así que su presencia no diría nada.
        """
        assert BACKENDS == {"sqlalchemy": "sqlalchemy", "beanie": "beanie"}


# ── La config ─────────────────────────────────────────────────────────────────
class TestConfig:
    def test_storage_por_default_es_none(self):
        from hexcore.darwin import IdentityConfig

        assert IdentityConfig(secret_key="k" * 48).storage is None

    def test_storage_explicito_se_respeta(self):
        from hexcore.darwin import IdentityConfig

        cfg = IdentityConfig(secret_key="k" * 48, storage="beanie")

        assert cfg.storage == "beanie"

    def test_storage_se_lee_del_entorno(self, monkeypatch):
        """
        Es una decisión de **despliegue**: la misma imagen puede correr contra Postgres en
        producción y contra Mongo en pruebas, y obligar a recompilar la config para eso sería
        absurdo.
        """
        from hexcore.darwin.application.config import STORAGE_ENV, IdentityConfig

        monkeypatch.setenv(STORAGE_ENV, "beanie")

        assert IdentityConfig(secret_key="k" * 48).storage == "beanie"

    def test_lo_explicito_gana_sobre_el_entorno(self, monkeypatch):
        from hexcore.darwin.application.config import STORAGE_ENV, IdentityConfig

        monkeypatch.setenv(STORAGE_ENV, "beanie")

        cfg = IdentityConfig(secret_key="k" * 48, storage="sqlalchemy")

        assert cfg.storage == "sqlalchemy"

    def test_la_config_no_valida_que_el_backend_este_instalado(self, monkeypatch):
        """
        A propósito: una `IdentityConfig` se construye también en un proceso que no va a tocar la
        base —el que sólo verifica tokens— y exigirle el extra ahí sería pedirle una dependencia
        que no usa. La validación es del contenedor, al resolver el primer repositorio.
        """
        from hexcore.darwin.application.config import STORAGE_ENV, IdentityConfig

        monkeypatch.setenv(STORAGE_ENV, "inventado")

        cfg = IdentityConfig(secret_key="k" * 48)

        assert cfg.storage == "inventado"


# ── El contrato de cada backend ───────────────────────────────────────────────
#: Los dos backends que Darwin shippea. El test corre sobre **los dos**, que es lo que atrapa el
#: backend nuevo al que le falta un nombre — la falla que aparece meses después, cuando alguien usa
#: ese repositorio.
BACKENDS_A_PROBAR = ("sqlalchemy", "beanie")


class TestContratoDelBackend:
    @pytest.mark.parametrize("backend", BACKENDS_A_PROBAR)
    def test_expone_los_cinco_nombres_del_nucleo(self, backend):
        """
        ⚠️ Un backend al que le falta uno falla recién cuando alguien usa ese repositorio, que
        puede ser meses después del despliegue.
        """
        pytest.importorskip(BACKENDS[backend])
        import importlib

        modulo = importlib.import_module(
            f"hexcore.darwin.infrastructure.orms.{backend}.repositories"
        )

        faltan = [n for n in CONTRATO_NUCLEO if not hasattr(modulo, n)]
        assert not faltan, f"le faltan al backend {backend}: {faltan}"

    @pytest.mark.parametrize("backend", BACKENDS_A_PROBAR)
    @pytest.mark.parametrize("plugin", sorted(CONTRATO_PLUGINS))
    def test_expone_los_nombres_de_cada_plugin(self, plugin, backend):
        """Los cuatro plugins con almacenamiento, en los dos backends: ocho combinaciones."""
        pytest.importorskip(BACKENDS[backend])
        import importlib

        modulo = importlib.import_module(
            f"hexcore.darwin.plugins.{plugin}.orms.{backend}.repository"
        )

        faltan = [n for n in CONTRATO_PLUGINS[plugin] if not hasattr(modulo, n)]
        assert not faltan, f"le faltan a {plugin}/{backend}: {faltan}"

    @pytest.mark.parametrize("backend", BACKENDS_A_PROBAR)
    def test_los_dos_backends_implementan_los_mismos_puertos(self, backend):
        """
        Los repositorios de cada backend son subclases de los puertos `Abstract*`. Es lo que hace
        que el contenedor pueda intercambiarlos sin saber cuál tiene.
        """
        pytest.importorskip(BACKENDS[backend])
        import importlib

        from hexcore.darwin.domain.ports import (
            AbstractAccountRepository,
            AbstractAuditSink,
            AbstractSessionRepository,
            AbstractUserRepository,
            AbstractVerificationRepository,
        )

        modulo = importlib.import_module(
            f"hexcore.darwin.infrastructure.orms.{backend}.repositories"
        )
        esperados = {
            "UserRepository": AbstractUserRepository,
            "SessionRepository": AbstractSessionRepository,
            "AccountRepository": AbstractAccountRepository,
            "VerificationRepository": AbstractVerificationRepository,
            "AuditSink": AbstractAuditSink,
        }

        for nombre, puerto in esperados.items():
            clase = getattr(modulo, nombre)
            assert issubclass(clase, puerto), (
                f"{backend}.{nombre} no implementa {puerto.__name__}"
            )

    def test_los_alias_apuntan_a_las_clases_con_prefijo(self):
        """
        Los dos nombres sirven: el prefijado dice en qué está implementado —útil para quien lo
        instancia a mano— y el neutro es el nombre del rol, que es lo que el contenedor busca.
        """
        pytest.importorskip("sqlalchemy")
        from hexcore.darwin.infrastructure.orms.sqlalchemy import repositories as sa

        assert sa.UserRepository is sa.SqlAlchemyUserRepository
        assert sa.AuditSink is sa.SqlAlchemyAuditSink

        pytest.importorskip("beanie")
        from hexcore.darwin.infrastructure.orms.beanie import repositories as be

        assert be.UserRepository is be.BeanieUserRepository
        assert be.AuditSink is be.BeanieAuditSink


# ── La frontera: el núcleo no arrastra ningún backend ─────────────────────────
class TestFrontera:
    @pytest.mark.parametrize(
        "modulo",
        [
            "hexcore.darwin",
            "hexcore.darwin.domain.context",
            "hexcore.darwin.domain.ports",
            "hexcore.darwin.application.config",
            "hexcore.darwin.application.container",
            "hexcore.darwin.application.services",
            "hexcore.darwin.infrastructure.tokens",
            "hexcore.darwin.infrastructure.transports",
            "hexcore.darwin.infrastructure.orms.selection",
            "hexcore.darwin.plugins.storage",
        ],
    )
    def test_el_nucleo_no_importa_ningun_backend(self, modulo):
        """
        ⚠️ **Es la frontera entera.** Si un módulo del núcleo necesita sqlalchemy o beanie
        —en el nivel superior o transitivamente— entonces `[darwin]` no es instalable sin ese
        extra, y la separación en tres piezas es una convención de nombres y nada más.

        Se prueba **bloqueando los dos** backends, no mirando `sys.modules`: ver `_sin_backends`.
        """
        resultado = _sin_backends(f"import {modulo}\nprint('ok')\n")

        assert resultado.returncode == 0, (
            f"{modulo} necesita un backend:\n" + resultado.stdout + resultado.stderr
        )

    @pytest.mark.parametrize(
        "plugin",
        ["magic_link", "two_factor", "oauth", "impersonate", "passkey", "organization"],
    )
    def test_nombrar_un_plugin_no_importa_ningun_backend(self, plugin):
        """
        Los seis, incluidos los cuatro que tienen tabla: sus mixins y repositorios viven en
        `orms/{backend}/` y los sirve un import perezoso.
        """
        resultado = _sin_backends(
            f"import hexcore.darwin.plugins.{plugin}\nprint('ok')\n"
        )

        assert resultado.returncode == 0, (
            f"el plugin {plugin} necesita un backend:\n"
            + resultado.stdout
            + resultado.stderr
        )

    @pytest.mark.parametrize(
        "plugin", ["two_factor", "oauth", "passkey", "organization"]
    )
    @pytest.mark.parametrize(
        "backend, bloqueado",
        [("sqlalchemy", "beanie"), ("beanie", "sqlalchemy")],
    )
    def test_el_backend_de_un_plugin_no_exige_el_otro(
        self, plugin, backend, bloqueado
    ):
        """
        La simetría, en los dos sentidos y para los cuatro plugins: ocho combinaciones.

        Cada backend exige **el suyo** y nada más. Si el de Mongo arrastrara sqlalchemy, un
        despliegue con `[darwin,darwin-beanie]` no podría cablear el plugin — y el síntoma
        aparecería al arrancar, con un `ModuleNotFoundError` que no explica por qué.
        """
        resultado = _bloqueando(
            bloqueado,
            f"import hexcore.darwin.plugins.{plugin}.orms.{backend}.repository\n"
            "print('ok')\n",
        )

        assert resultado.returncode == 0, (
            f"{plugin}/{backend} exige {bloqueado}:\n"
            + resultado.stdout
            + resultado.stderr
        )

    def test_el_paquete_orms_no_importa_ninguno_de_los_dos(self):
        """
        `import ...orms` en un proceso que sólo tiene uno de los extras no puede fallar: el
        `__init__` está vacío de imports a propósito.
        """
        resultado = _sin_backends(
            "import hexcore.darwin.infrastructure.orms\nprint('ok')\n"
        )

        assert resultado.returncode == 0, resultado.stdout + resultado.stderr

    def test_el_kit_de_testing_tampoco(self):
        """Los fakes existen justamente para no necesitar ningún backend."""
        resultado = _sin_backends(
            "import hexcore.darwin.testing as m\n"
            "assert m.make_user('a@b.c').email == 'a@b.c'\n"
            "print('ok')\n"
        )

        assert resultado.returncode == 0, resultado.stdout + resultado.stderr


# ── El contenedor ─────────────────────────────────────────────────────────────
class TestContenedor:
    def test_resuelve_y_cachea_el_backend(self):
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

        reset_identity()
        contenedor = configure_identity(
            IdentityConfig(storage="sqlalchemy", secret_key="k" * 48)
        )
        try:
            assert contenedor.storage_backend == "sqlalchemy"
            # Cacheado: la segunda lectura no vuelve a resolver.
            assert contenedor.storage_backend == "sqlalchemy"
        finally:
            reset_identity()

    def test_los_repositorios_salen_del_backend_resuelto(self):
        pytest.importorskip("sqlalchemy")
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

        reset_identity()
        contenedor = configure_identity(
            IdentityConfig(storage="sqlalchemy", secret_key="k" * 48)
        )
        try:
            assert type(contenedor.users()).__name__ == "SqlAlchemyUserRepository"
            assert (
                type(contenedor.sessions_repository()).__name__
                == "SqlAlchemySessionRepository"
            )
            assert type(contenedor.accounts()).__name__ == "SqlAlchemyAccountRepository"
            assert (
                type(contenedor.verifications()).__name__
                == "SqlAlchemyVerificationRepository"
            )
        finally:
            reset_identity()

    def test_un_backend_no_instalado_falla_con_la_remediacion(self, monkeypatch):
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity
        from hexcore.darwin.infrastructure.orms import selection

        # Se simula que `beanie` no está apuntándolo a un paquete inexistente. **Se lo deja en
        # el mapa**: sacarlo haría que el error fuera "ese backend no existe" en vez de "ese
        # backend no está instalado", que son dos remediaciones distintas.
        monkeypatch.setattr(
            selection,
            "BACKENDS",
            {"sqlalchemy": "sqlalchemy", "beanie": "beanie_que_no_existe"},
        )

        reset_identity()
        contenedor = configure_identity(
            IdentityConfig(storage="beanie", secret_key="k" * 48)
        )
        try:
            with pytest.raises(ValueError) as excinfo:
                contenedor.users()

            assert "pip install 'hexcore[darwin-beanie]'" in str(excinfo.value)
        finally:
            reset_identity()

    def test_un_puerto_inyectado_no_resuelve_ningun_backend(self):
        """
        El kit de testing inyecta los cinco, así que un test con `configure_test_identity` nunca
        toca la resolución — y por eso funciona sin ningún extra instalado.
        """
        from hexcore.darwin import reset_identity
        from hexcore.darwin.testing import configure_test_identity, make_user

        reset_identity()
        contenedor = configure_test_identity(seed_users=[make_user("ana@ejemplo.com")])
        try:
            assert type(contenedor.users()).__name__ == "FakeUserRepository"
        finally:
            reset_identity()


# ── El resolvedor de los plugins ──────────────────────────────────────────────
class TestPluginStorage:
    def test_le_pregunta_al_contenedor(self):
        """
        Y no resuelve por su cuenta: si detectara aparte, un despliegue con los dos extras podría
        terminar con el núcleo en un backend y un plugin en el otro — y el síntoma es que el login
        funciona y el segundo factor no encuentra nada.
        """
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity
        from hexcore.darwin.plugins.storage import plugin_storage_backend

        reset_identity()
        configure_identity(IdentityConfig(storage="sqlalchemy", secret_key="k" * 48))
        try:
            assert plugin_storage_backend() == "sqlalchemy"
        finally:
            reset_identity()

    def test_devuelve_el_modulo_del_backend(self):
        pytest.importorskip("sqlalchemy")
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity
        from hexcore.darwin.plugins.storage import plugin_repositories

        reset_identity()
        configure_identity(IdentityConfig(storage="sqlalchemy", secret_key="k" * 48))
        try:
            modulo = plugin_repositories("two_factor")

            assert modulo.__name__.endswith("orms.sqlalchemy.repository")
            assert hasattr(modulo, "TwoFactorRepository")
        finally:
            reset_identity()

    @pytest.mark.parametrize("backend", ["sqlalchemy", "beanie"])
    @pytest.mark.parametrize(
        "plugin", ["two_factor", "oauth", "passkey", "organization"]
    )
    def test_los_cuatro_plugins_tienen_los_dos_backends(self, plugin, backend):
        """
        Las ocho combinaciones se resuelven. Si un plugin quedara con un solo backend, el
        despliegue que use el otro fallaría al cablearlo.
        """
        pytest.importorskip(BACKENDS[backend])
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity
        from hexcore.darwin.plugins.storage import plugin_repositories

        reset_identity()
        configure_identity(IdentityConfig(storage=backend, secret_key="k" * 48))
        try:
            modulo = plugin_repositories(plugin)

            assert modulo.__name__.endswith(f"orms.{backend}.repository")
        finally:
            reset_identity()

    def test_un_plugin_sin_ese_backend_da_un_error_util(self):
        """
        El caso real: un plugin de terceros puede shippear sólo SQL, y quien lo cablea con Mongo
        tiene que enterarse al arrancar y no con un `ModuleNotFoundError` sobre un submódulo que
        nunca nombró.

        Se usa un nombre de plugin que no existe, porque los cuatro que shippea Darwin tienen los
        dos backends — que es justamente lo que fija el test de arriba.
        """
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity
        from hexcore.darwin.plugins.storage import plugin_repositories

        reset_identity()
        configure_identity(IdentityConfig(storage="beanie", secret_key="k" * 48))
        try:
            with pytest.raises(ImportError) as excinfo:
                plugin_repositories("un_plugin_de_terceros")

            mensaje = str(excinfo.value)
            assert "no implementa el backend 'beanie'" in mensaje
            assert "repository=" in mensaje, "la remediación tiene que ser copiable"
        finally:
            reset_identity()
