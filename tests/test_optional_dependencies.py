import subprocess
import sys
import pytest

@pytest.mark.parametrize("module_to_hide, module_to_import", [
    ("sqlalchemy", "hexcore.infrastructure.uow"),
    ("beanie", "hexcore.infrastructure.uow"),
    ("sqlalchemy", "hexcore.infrastructure.repositories.implementations"),
    ("beanie", "hexcore.infrastructure.repositories.implementations"),
    ("sqlalchemy", "hexcore.infrastructure.repositories.utils"),
    ("beanie", "hexcore.infrastructure.repositories.utils"),
    # Los scopes de F3 no deben arrastrar SQLAlchemy en import time.
    ("sqlalchemy", "hexcore.infrastructure.uow.scopes"),
    # El módulo de contexto y el de resolución de FQN son stdlib puro.
    ("sqlalchemy", "hexcore.domain.cqrs.context"),
    ("sqlalchemy", "hexcore.domain.cqrs.resolution"),
    ("fastapi", "hexcore.domain.cqrs"),
    ("fastapi", "hexcore.application.cqrs"),
    # ── Darwin: el dominio de identidad es stdlib + pydantic y nada más ────────
    # La fachada resuelve perezosamente, así que importarla no puede arrastrar ningún
    # extra. Y el contexto lo importan el middleware de CQRS y los command handlers, o sea
    # que tiene que funcionar en un proceso sin `[sql]`, sin `[api]` y sin `[darwin]`.
    ("sqlalchemy", "hexcore.darwin"),
    ("fastapi", "hexcore.darwin"),
    ("joserfc", "hexcore.darwin"),
    ("argon2", "hexcore.darwin"),
    ("sqlalchemy", "hexcore.darwin.domain.context"),
    ("fastapi", "hexcore.darwin.domain.context"),
    ("sqlalchemy", "hexcore.darwin.domain.exceptions"),
    ("sqlalchemy", "hexcore.darwin.domain.value_objects"),
    ("sqlalchemy", "hexcore.darwin.domain.permissions"),
    ("sqlalchemy", "hexcore.darwin.domain.ports"),
    ("sqlalchemy", "hexcore.darwin.domain.entities"),
    ("sqlalchemy", "hexcore.darwin.domain.events"),
    # La fachada expone la capa de persistencia, pero resuelve perezosamente: nombrarla
    # no puede arrastrar sqlalchemy hasta que pidas un modelo.
    ("joserfc", "hexcore.darwin.domain.entities"),
    ("argon2", "hexcore.darwin.domain.ports"),
    # El reloj y la revocación son de infraestructura pero no tocan crypto ni sqlalchemy:
    # los importa el middleware de CQRS y tienen que funcionar en un proceso pelado.
    ("joserfc", "hexcore.darwin.infrastructure.clock"),
    ("argon2", "hexcore.darwin.infrastructure.clock"),
    ("sqlalchemy", "hexcore.darwin.infrastructure.clock"),
    ("joserfc", "hexcore.darwin.infrastructure.revocation"),
    ("argon2", "hexcore.darwin.infrastructure.revocation"),
    ("sqlalchemy", "hexcore.darwin.infrastructure.revocation"),
    # La configuración la importa `hexcore.config`, que importa medio framework: tiene que
    # ser stdlib + pydantic y nada más.
    ("sqlalchemy", "hexcore.darwin.application.config"),
    ("fastapi", "hexcore.darwin.application.config"),
    ("joserfc", "hexcore.darwin.application.config"),
    ("argon2", "hexcore.darwin.application.config"),
    # El contenedor resuelve sus adaptadores perezosamente, así que importarlo no puede
    # arrastrar ninguno de los extras que esos adaptadores necesitan.
    ("sqlalchemy", "hexcore.darwin.application.container"),
    ("joserfc", "hexcore.darwin.application.container"),
    ("argon2", "hexcore.darwin.application.container"),
    ("fastapi", "hexcore.darwin.application.container"),
    # El sobre que cruza la cola: lo importa `configure_identity`, y su restaurador lo
    # invoca el `CQRSConsumer`, que corre en workers sin extras de API. El códec es
    # `hmac` + `json` de la stdlib a propósito y no joserfc: el sobre no es un JWT.
    ("sqlalchemy", "hexcore.darwin.infrastructure.envelope"),
    ("joserfc", "hexcore.darwin.infrastructure.envelope"),
    ("argon2", "hexcore.darwin.infrastructure.envelope"),
    ("fastapi", "hexcore.darwin.infrastructure.envelope"),
    # El punto de extensión del núcleo lo importan el serializer y los cinco transportes.
    # (No se chequea contra pydantic: no es opcional — `hexcore/__init__.py` lo importa
    # eager, así que esconderlo rompe cualquier import del paquete.)
    ("sqlalchemy", "hexcore.domain.cqrs.envelope"),
    ("fastapi", "hexcore.domain.cqrs.envelope"),
    # El borde HTTP (Fase 7) SÍ necesita `[api]` —es Starlette— pero no debe arrastrar los
    # otros extras: un proceso web sin `[sql]` tiene que poder importar los transportes.
    ("joserfc", "hexcore.darwin.infrastructure.transports"),
    ("argon2", "hexcore.darwin.infrastructure.transports"),
    ("sqlalchemy", "hexcore.darwin.infrastructure.transports"),
    ("joserfc", "hexcore.darwin.infrastructure.api.middlewares"),
    ("argon2", "hexcore.darwin.infrastructure.api.middlewares"),
    ("sqlalchemy", "hexcore.darwin.infrastructure.api.middlewares"),
    ("joserfc", "hexcore.darwin.infrastructure.api.dependencies"),
    ("sqlalchemy", "hexcore.darwin.infrastructure.api.dependencies"),
    # El lifespan lo importa el arranque de la app; no puede exigir crypto para existir.
    ("joserfc", "hexcore.darwin.infrastructure.lifespan"),
    ("argon2", "hexcore.darwin.infrastructure.lifespan"),
    # ── Fase 8: los plugins ───────────────────────────────────────────────────
    # El contrato de plugin y el registro son stdlib puro, y tienen que serlo: los importa el
    # `HookMiddleware`, que corre en el pipeline de cualquier proceso — incluido un worker sin
    # `[api]` ni `[darwin]`.
    ("sqlalchemy", "hexcore.darwin.domain.plugins"),
    ("fastapi", "hexcore.darwin.domain.plugins"),
    ("joserfc", "hexcore.darwin.domain.plugins"),
    ("argon2", "hexcore.darwin.domain.plugins"),
    ("sqlalchemy", "hexcore.darwin.application.plugins"),
    ("fastapi", "hexcore.darwin.application.plugins"),
    ("joserfc", "hexcore.darwin.application.plugins"),
    ("argon2", "hexcore.darwin.application.plugins"),
    ("sqlalchemy", "hexcore.darwin.application.hooks"),
    ("fastapi", "hexcore.darwin.application.hooks"),
    ("joserfc", "hexcore.darwin.application.hooks"),
    ("argon2", "hexcore.darwin.application.hooks"),
    # El plugin de referencia declara sus comandos y su hook sin tocar ningún extra: el
    # router y los repositorios los importa adentro de las funciones. Si esto se rompiera,
    # nombrar un plugin en la config arrastraría medio framework.
    ("sqlalchemy", "hexcore.darwin.plugins.magic_link"),
    ("fastapi", "hexcore.darwin.plugins.magic_link"),
    ("joserfc", "hexcore.darwin.plugins.magic_link"),
    ("argon2", "hexcore.darwin.plugins.magic_link"),
    ("sqlalchemy", "hexcore.darwin.plugins.magic_link.commands"),
    ("fastapi", "hexcore.darwin.plugins.magic_link.commands"),
    ("joserfc", "hexcore.darwin.plugins.magic_link.commands"),
    ("argon2", "hexcore.darwin.plugins.magic_link.commands"),
    # ── Fase 9: two_factor ────────────────────────────────────────────────────
    # El TOTP es `hmac` de la stdlib: no depende de ningún extra, ni siquiera del de crypto.
    # Es lo que permite que un worker sin `[darwin]` importe el módulo para generar un código
    # en un script.
    ("sqlalchemy", "hexcore.darwin.plugins.two_factor.totp"),
    ("fastapi", "hexcore.darwin.plugins.two_factor.totp"),
    ("joserfc", "hexcore.darwin.plugins.two_factor.totp"),
    ("argon2", "hexcore.darwin.plugins.two_factor.totp"),
    # El dominio del plugin: entidad, puerto y excepciones. Lo importa el borde HTTP para
    # mapear los status, así que no puede exigir sqlalchemy.
    ("sqlalchemy", "hexcore.darwin.plugins.two_factor.domain"),
    ("fastapi", "hexcore.darwin.plugins.two_factor.domain"),
    ("joserfc", "hexcore.darwin.plugins.two_factor.domain"),
    ("argon2", "hexcore.darwin.plugins.two_factor.domain"),
    # El cifrado del secreto usa joserfc **adentro de los métodos**: nombrar la clase no lo
    # arrastra, que es lo que permite importar el plugin en un proceso sin el extra.
    ("sqlalchemy", "hexcore.darwin.plugins.two_factor.crypto"),
    ("fastapi", "hexcore.darwin.plugins.two_factor.crypto"),
    ("argon2", "hexcore.darwin.plugins.two_factor.crypto"),
    # El plugin y su servicio resuelven todo perezosamente.
    ("sqlalchemy", "hexcore.darwin.plugins.two_factor"),
    ("fastapi", "hexcore.darwin.plugins.two_factor"),
    ("joserfc", "hexcore.darwin.plugins.two_factor"),
    ("argon2", "hexcore.darwin.plugins.two_factor"),
    ("sqlalchemy", "hexcore.darwin.plugins.two_factor.service"),
    ("fastapi", "hexcore.darwin.plugins.two_factor.service"),
    ("joserfc", "hexcore.darwin.plugins.two_factor.service"),
    ("argon2", "hexcore.darwin.plugins.two_factor.service"),
    ("sqlalchemy", "hexcore.darwin.plugins.two_factor.commands"),
    ("fastapi", "hexcore.darwin.plugins.two_factor.commands"),
    ("joserfc", "hexcore.darwin.plugins.two_factor.commands"),
    ("argon2", "hexcore.darwin.plugins.two_factor.commands"),
    # La caja de cifrado usa joserfc **adentro de los métodos**: nombrar la clase no lo
    # arrastra. Es lo que permite importarla desde un módulo de dominio.
    ("sqlalchemy", "hexcore.darwin.infrastructure.secretbox"),
    ("fastapi", "hexcore.darwin.infrastructure.secretbox"),
    ("argon2", "hexcore.darwin.infrastructure.secretbox"),
    # ── Fase 9: oauth ─────────────────────────────────────────────────────────
    # PKCE es `hashlib` + `secrets`, y el puerto HTTP es una ABC: nada de esto depende de
    # httpx, que va en `[darwin-oauth]`. Es lo que permite que el borde HTTP importe el mapa
    # de excepciones sin el extra, y que un test del flujo no necesite red.
    ("httpx", "hexcore.darwin.plugins.oauth.domain"),
    ("sqlalchemy", "hexcore.darwin.plugins.oauth.domain"),
    ("fastapi", "hexcore.darwin.plugins.oauth.domain"),
    ("joserfc", "hexcore.darwin.plugins.oauth.domain"),
    ("argon2", "hexcore.darwin.plugins.oauth.domain"),
    # Los descriptores de proveedor son pydantic y `urllib`.
    ("httpx", "hexcore.darwin.plugins.oauth.providers"),
    ("sqlalchemy", "hexcore.darwin.plugins.oauth.providers"),
    ("fastapi", "hexcore.darwin.plugins.oauth.providers"),
    ("joserfc", "hexcore.darwin.plugins.oauth.providers"),
    # El plugin y su servicio resuelven todo perezosamente. `httpx` en particular: un
    # despliegue que cablea el plugin con su propio cliente HTTP no tiene por qué instalarlo.
    ("httpx", "hexcore.darwin.plugins.oauth"),
    ("sqlalchemy", "hexcore.darwin.plugins.oauth"),
    ("fastapi", "hexcore.darwin.plugins.oauth"),
    ("joserfc", "hexcore.darwin.plugins.oauth"),
    ("argon2", "hexcore.darwin.plugins.oauth"),
    ("httpx", "hexcore.darwin.plugins.oauth.service"),
    ("sqlalchemy", "hexcore.darwin.plugins.oauth.service"),
    ("fastapi", "hexcore.darwin.plugins.oauth.service"),
    ("joserfc", "hexcore.darwin.plugins.oauth.service"),
    # El adaptador de httpx: importarlo no lo exige, porque el `import httpx` está adentro de
    # `_abrir`. Es lo que hace que el error sea el mensaje con `pip install` y no un
    # `ModuleNotFoundError` en el arranque.
    ("httpx", "hexcore.darwin.plugins.oauth.http"),
    ("sqlalchemy", "hexcore.darwin.plugins.oauth.http"),
    ("fastapi", "hexcore.darwin.plugins.oauth.http"),
    # ── Fase 9: impersonate ───────────────────────────────────────────────────
    # No aporta tabla —la impersonación está modelada en `session` desde la Fase 3— y su
    # dominio es la política más las excepciones. El borde HTTP importa el mapa de status, así
    # que nada de esto puede exigir un extra.
    ("sqlalchemy", "hexcore.darwin.plugins.impersonate.domain"),
    ("fastapi", "hexcore.darwin.plugins.impersonate.domain"),
    ("joserfc", "hexcore.darwin.plugins.impersonate.domain"),
    ("argon2", "hexcore.darwin.plugins.impersonate.domain"),
    ("sqlalchemy", "hexcore.darwin.plugins.impersonate"),
    ("fastapi", "hexcore.darwin.plugins.impersonate"),
    ("joserfc", "hexcore.darwin.plugins.impersonate"),
    ("argon2", "hexcore.darwin.plugins.impersonate"),
    ("sqlalchemy", "hexcore.darwin.plugins.impersonate.service"),
    ("fastapi", "hexcore.darwin.plugins.impersonate.service"),
    ("joserfc", "hexcore.darwin.plugins.impersonate.service"),
    ("argon2", "hexcore.darwin.plugins.impersonate.service"),
    ("sqlalchemy", "hexcore.darwin.plugins.impersonate.commands"),
    ("fastapi", "hexcore.darwin.plugins.impersonate.commands"),
    ("joserfc", "hexcore.darwin.plugins.impersonate.commands"),
    ("argon2", "hexcore.darwin.plugins.impersonate.commands"),
    # ── Fase 9: passkey ───────────────────────────────────────────────────────
    # El dominio son las entidades, los tres puertos y las excepciones: nada de webauthn ni de
    # sqlalchemy. Lo importa el borde HTTP para mapear los status, y un test del flujo lo usa
    # con un doble del verificador.
    ("webauthn", "hexcore.darwin.plugins.passkey.domain"),
    ("sqlalchemy", "hexcore.darwin.plugins.passkey.domain"),
    ("fastapi", "hexcore.darwin.plugins.passkey.domain"),
    ("joserfc", "hexcore.darwin.plugins.passkey.domain"),
    ("argon2", "hexcore.darwin.plugins.passkey.domain"),
    # El plugin y su servicio resuelven todo perezosamente, incluidos los dos mixins —que están
    # en `__all__` y los sirve un `__getattr__` de módulo, porque importarlos arrastra
    # sqlalchemy.
    ("webauthn", "hexcore.darwin.plugins.passkey"),
    ("sqlalchemy", "hexcore.darwin.plugins.passkey"),
    ("fastapi", "hexcore.darwin.plugins.passkey"),
    ("joserfc", "hexcore.darwin.plugins.passkey"),
    ("argon2", "hexcore.darwin.plugins.passkey"),
    ("webauthn", "hexcore.darwin.plugins.passkey.service"),
    ("sqlalchemy", "hexcore.darwin.plugins.passkey.service"),
    ("fastapi", "hexcore.darwin.plugins.passkey.service"),
    ("joserfc", "hexcore.darwin.plugins.passkey.service"),
    # El adaptador de py_webauthn: importarlo no lo exige, porque los `import webauthn` están
    # adentro de los métodos. Es lo que hace que un despliegue con su propio verificador no
    # necesite el extra.
    ("webauthn", "hexcore.darwin.plugins.passkey.webauthn_adapter"),
    ("sqlalchemy", "hexcore.darwin.plugins.passkey.webauthn_adapter"),
    ("fastapi", "hexcore.darwin.plugins.passkey.webauthn_adapter"),
    # ── Fase 9: organization ──────────────────────────────────────────────────
    # No agrega dependencias. El dominio son las entidades, los roles, los tres puertos y las
    # excepciones: lo importa el borde HTTP para mapear los status.
    ("sqlalchemy", "hexcore.darwin.plugins.organization.domain"),
    ("fastapi", "hexcore.darwin.plugins.organization.domain"),
    ("joserfc", "hexcore.darwin.plugins.organization.domain"),
    ("argon2", "hexcore.darwin.plugins.organization.domain"),
    # El plugin y su servicio resuelven todo perezosamente, incluidos los tres mixins.
    ("sqlalchemy", "hexcore.darwin.plugins.organization"),
    ("fastapi", "hexcore.darwin.plugins.organization"),
    ("joserfc", "hexcore.darwin.plugins.organization"),
    ("argon2", "hexcore.darwin.plugins.organization"),
    ("sqlalchemy", "hexcore.darwin.plugins.organization.service"),
    ("fastapi", "hexcore.darwin.plugins.organization.service"),
    ("joserfc", "hexcore.darwin.plugins.organization.service"),
    ("argon2", "hexcore.darwin.plugins.organization.service"),
    # ── Fase 10: los kits de testing ──────────────────────────────────────────
    # El kit genérico son dos dobles sobre los puertos de dominio: nada de sqlalchemy. Es lo que
    # permite que un consumidor sin `[sql]` pruebe sus casos de uso.
    ("sqlalchemy", "hexcore.testing.repositories"),
    ("fastapi", "hexcore.testing.repositories"),
    ("sqlalchemy", "hexcore.testing"),
    ("fastapi", "hexcore.testing"),
    # El kit de Darwin: los fakes existen justamente para no necesitar el extra. `joserfc` sí lo
    # necesita el cableado completo —emite tokens de verdad— pero se importa adentro de la
    # función, así que nombrar el módulo no lo exige.
    ("sqlalchemy", "hexcore.darwin.testing"),
    ("fastapi", "hexcore.darwin.testing"),
    ("joserfc", "hexcore.darwin.testing"),
    ("argon2", "hexcore.darwin.testing"),
    ("sqlalchemy", "hexcore.darwin.testing.fakes"),
    ("fastapi", "hexcore.darwin.testing.fakes"),
    ("joserfc", "hexcore.darwin.testing.fakes"),
    ("argon2", "hexcore.darwin.testing.fakes"),
    ("sqlalchemy", "hexcore.darwin.testing.helpers"),
    ("fastapi", "hexcore.darwin.testing.helpers"),
    ("joserfc", "hexcore.darwin.testing.helpers"),
    ("argon2", "hexcore.darwin.testing.helpers"),
    # ── La separacion en tres piezas ──────────────────────────────────────────
    # `[darwin]` es el nucleo sin almacenamiento, y estas filas son el contrato: ni el selector
    # ni el resolvedor de plugins pueden exigir un backend, porque son justamente los que
    # eligen cual usar.
    ("sqlalchemy", "hexcore.darwin.infrastructure.orms.selection"),
    ("beanie", "hexcore.darwin.infrastructure.orms.selection"),
    ("sqlalchemy", "hexcore.darwin.infrastructure.orms"),
    ("beanie", "hexcore.darwin.infrastructure.orms"),
    ("sqlalchemy", "hexcore.darwin.plugins.storage"),
    ("beanie", "hexcore.darwin.plugins.storage"),
    # Y el nucleo entero sin ninguno de los dos: es lo que hace que la separacion sea real.
    ("beanie", "hexcore.darwin"),
    ("beanie", "hexcore.darwin.application.container"),
    ("beanie", "hexcore.darwin.application.services"),
    ("beanie", "hexcore.darwin.infrastructure.tokens"),
    ("beanie", "hexcore.darwin.testing"),
    # La sub-app de Typer la arrastra `hexcore/__init__.py` **eager** vía
    # `hexcore.infrastructure.cli`: si importara algo de Darwin en el nivel superior, un
    # `import hexcore` en un proceso pelado se caería. Es el contrato más frágil de la fase.
    ("sqlalchemy", "hexcore.darwin.infrastructure.cli"),
    ("fastapi", "hexcore.darwin.infrastructure.cli"),
    ("joserfc", "hexcore.darwin.infrastructure.cli"),
    ("argon2", "hexcore.darwin.infrastructure.cli"),
])
def test_optional_dependencies_do_not_crash_imports(module_to_hide, module_to_import):
    """
    Este test verifica que si una dependencia opcional como SQLAlchemy o Beanie no está
    instalada en el entorno (simulado bloqueando su importación), la carga de los
    módulos base de infraestructura de HexCore no fallará con ImportError.
    """
    
    # El código inyectado bloquea la importación del módulo objetivo y luego intenta importar HexCore.
    code = f"""
import sys

class MockFinder:
    @classmethod
    def find_spec(cls, fullname, path, target=None):
        if fullname.split(".")[0] == "{module_to_hide}":
            raise ImportError("Mocked ImportError for " + fullname)
        return None

sys.meta_path.insert(0, MockFinder)

# Si esto no lanza ImportError, el test pasa.
import {module_to_import}
print("Imported {module_to_import} successfully without {module_to_hide}")
"""

    import os
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env
    )
    
    assert result.returncode == 0, f"Import falló: {result.stderr}"
    assert "Imported" in result.stdout
