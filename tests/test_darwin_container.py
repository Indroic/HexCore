"""
Darwin Fase 5: el contenedor y el registro de handlers.

El contenedor copia la forma de `CQRSContainer` a propósito, así que estos tests son los mismos
que `test_cqrs_factory.py` hace con el otro: sin configurar → error con remediación,
reconfigurar reemplaza, init perezoso thread-safe, y los `provide_*` resuelven.

Que la forma sea la misma no es simetría estética: quien ya sabe cablear CQRS en HexCore no
tiene que aprender un segundo patrón.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from hexcore.darwin import (  # noqa: E402
    FixedClock,
    IdentityConfig,
    IdentityContainer,
    StaticKeyStore,
    configure_identity,
    generate_signing_key,
    get_identity_container,
    provide_identity,
    provide_identity_config,
    provide_session_service,
    register_identity_handlers,
    reset_identity,
)

CLAVE = "k" * 48
AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _sin_contenedor():
    """
    Descarta el contenedor antes y después de cada test.

    Antes **y** después: el contenedor es estado global de proceso, así que un test que lo deje
    configurado hace que el siguiente pase por el motivo equivocado — y el que verifica el error
    de "sin configurar" pasaría a fallar según el orden de ejecución.
    """
    reset_identity()
    yield
    reset_identity()


def _config() -> IdentityConfig:
    return IdentityConfig(secret_key=CLAVE)


def _componentes() -> dict:
    """Componentes inyectados que no tocan la base ni generan claves nuevas."""
    return {
        "clock": FixedClock(AHORA),
        "key_store": StaticKeyStore([generate_signing_key(kid="k1")]),
    }


# ── Sin configurar ────────────────────────────────────────────────────────────
def test_sin_configurar_lanza_con_remediacion():
    """
    El mensaje tiene que traer la línea exacta que falta, no "no configurado".

    Es el mismo criterio que `get_cqrs_container()`: un error de cableado se descubre al
    arrancar, y el mensaje es la documentación que el desarrollador va a leer en ese momento.
    """
    with pytest.raises(RuntimeError) as excinfo:
        get_identity_container()

    mensaje = str(excinfo.value)
    assert "configure_identity" in mensaje
    assert "IdentityConfig" in mensaje
    # Y menciona el caso de test, que es donde más se topa uno con esto.
    assert "reset_identity" in mensaje


@pytest.mark.parametrize(
    "provider", [provide_identity, provide_session_service, provide_identity_config]
)
def test_los_providers_tambien_lanzan_sin_configurar(provider):
    with pytest.raises(RuntimeError, match="configure_identity"):
        provider()


# ── Configurar ────────────────────────────────────────────────────────────────
def test_configure_devuelve_el_contenedor_y_lo_publica():
    contenedor = configure_identity(_config(), **_componentes())

    assert isinstance(contenedor, IdentityContainer)
    assert get_identity_container() is contenedor


def test_reconfigurar_reemplaza_el_contenedor():
    """
    Reemplaza en vez de mutar. Mutar dejaría los servicios ya cacheados apuntando a los
    componentes viejos, y el síntoma sería que la reconfiguración "no tomó" en la mitad de las
    llamadas.
    """
    primero = configure_identity(_config(), **_componentes())
    primero.identity_service()  # fuerza el cacheo

    segundo = configure_identity(_config(), **_componentes())

    assert segundo is not primero
    assert get_identity_container() is segundo


def test_reset_descarta_el_contenedor():
    configure_identity(_config(), **_componentes())
    reset_identity()

    with pytest.raises(RuntimeError):
        get_identity_container()


def test_configure_toma_la_config_de_server_config():
    """`ServerConfig.darwin`, igual que `configure_cqrs` toma `ServerConfig.cqrs`."""
    from hexcore.config import LazyConfig, ServerConfig

    previo = LazyConfig._imported_config
    esperada = _config()
    LazyConfig._imported_config = ServerConfig(debug=False, darwin=esperada)
    try:
        contenedor = configure_identity(**_componentes())
        assert contenedor.config is esperada
    finally:
        LazyConfig._imported_config = previo


# ── Pereza y cacheo ───────────────────────────────────────────────────────────
def test_configure_no_construye_nada():
    """
    Perezoso a propósito: `configure_identity()` se puede llamar en import time sin tocar la
    base ni generar claves. El trabajo real ocurre al primer uso.
    """
    contenedor = configure_identity(_config(), **_componentes())

    assert contenedor._identity_service is None
    assert contenedor._session_service is None
    assert contenedor._issuer is None
    assert contenedor._verifier is None


def test_los_componentes_se_cachean():
    contenedor = configure_identity(_config(), **_componentes())

    assert contenedor.identity_service() is contenedor.identity_service()
    assert contenedor.session_service() is contenedor.session_service()
    assert contenedor.issuer() is contenedor.issuer()
    assert contenedor.verifier() is contenedor.verifier()
    assert contenedor.hasher() is contenedor.hasher()


def test_los_componentes_inyectados_se_respetan():
    reloj = FixedClock(AHORA)
    almacen = StaticKeyStore([generate_signing_key(kid="inyectada")])

    contenedor = configure_identity(_config(), clock=reloj, key_store=almacen)

    assert contenedor.clock() is reloj
    assert contenedor.key_store() is almacen


def test_el_verificador_solo_acepta_el_algoritmo_configurado():
    """
    La allowlist por defecto acepta cinco algoritmos; el contenedor la estrecha al que de verdad
    se usa. Es la configuración más restrictiva posible sin dejar de funcionar.
    """
    from hexcore.darwin.application.config import TokenConfig

    contenedor = configure_identity(
        IdentityConfig(secret_key=CLAVE, tokens=TokenConfig(algorithm="Ed25519")),
        **_componentes(),
    )
    verificador = contenedor.verifier()

    assert verificador._allowed == ("Ed25519",)


def test_la_inicializacion_perezosa_es_thread_safe():
    """
    El `RLock` no es decorativo: sin él, dos hilos que piden el servicio a la vez construyen dos
    instancias y una se descarta — con lo cual el "cacheado" deja de ser único, que es
    precisamente su contrato.

    Es el mismo requisito que el resto del repo trata como no negociable (`HandlerRegistry`,
    `CQRSContainer`, `session`).
    """
    contenedor = configure_identity(_config(), **_componentes())

    with ThreadPoolExecutor(max_workers=16) as pool:
        instancias = list(pool.map(lambda _: contenedor.identity_service(), range(64)))

    assert len({id(x) for x in instancias}) == 1


def test_el_lock_es_reentrante():
    """
    Tiene que serlo: un componente construye otros. `session_service()` pide `issuer()`, que
    pide `key_store()`, y todo pasa por el mismo lock. Con un `Lock` a secas esto deadlockea.
    """
    contenedor = configure_identity(_config(), **_componentes())

    assert contenedor.session_service() is not None


# ── Validación del modelo de usuario ──────────────────────────────────────────
def test_configure_valida_el_modelo_de_usuario():
    """
    Al **configurar**, no en el primer login. Mismo criterio que
    `CQRSFactory._assert_enqueuer_for_background_commands`: un error de cableado descubierto en
    el primer request de producción ya llegó tarde.
    """
    pytest.importorskip("sqlalchemy")

    class NoEsUsuario:
        pass

    with pytest.raises(TypeError, match="UserMixin"):
        configure_identity(
            IdentityConfig(secret_key=CLAVE, user_model=NoEsUsuario), **_componentes()
        )


def test_configure_acepta_el_modelo_por_defecto():
    pytest.importorskip("sqlalchemy")
    from hexcore.darwin import UserModel

    contenedor = configure_identity(
        IdentityConfig(secret_key=CLAVE, user_model=UserModel), **_componentes()
    )

    assert contenedor.config.user_model is UserModel


# ── Registro de handlers ──────────────────────────────────────────────────────
def test_register_identity_handlers_registra_todo():
    from hexcore.cqrs import HandlerRegistry
    from hexcore.darwin import (
        AuthenticateToken,
        ChangePassword,
        ListActiveSessions,
        RefreshSession,
        SignIn,
        SignOut,
        SignOutEverywhere,
        SignUp,
        VerifyEmail,
    )
    from hexcore.darwin.application.commands import IssueVerificationCode

    registry = register_identity_handlers(HandlerRegistry())

    comandos = {
        SignUp,
        VerifyEmail,
        SignIn,
        RefreshSession,
        SignOut,
        SignOutEverywhere,
        ChangePassword,
        IssueVerificationCode,
    }
    assert comandos <= registry.registered_commands
    assert {AuthenticateToken, ListActiveSessions} <= registry.registered_queries


def test_register_es_fluido():
    from hexcore.cqrs import HandlerRegistry

    registry = HandlerRegistry()

    assert register_identity_handlers(registry) is registry


def test_los_handlers_se_registran_como_factories():
    """
    Como factories y no como instancias: el registry las invoca en el primer `resolve`, así que
    el contenedor tiene que estar configurado recién entonces y no al registrar. Eso permite
    llamar a `register_identity_handlers` en import time, que es donde uno quiere el cableado.
    """
    from hexcore.cqrs import HandlerRegistry
    from hexcore.darwin import SignUp

    registry = register_identity_handlers(HandlerRegistry())

    # Sin contenedor configurado, registrar no falló. Resolver el handler tampoco —el servicio
    # se resuelve al usarlo, no al construirlo.
    handler = registry.resolve_command_handler(SignUp)
    assert handler is not None


def test_ningun_comando_de_auth_va_a_background():
    """
    Un sign-in encolado devuelve al cliente antes de saber si las credenciales eran válidas, con
    lo cual no puede responder ni 200 ni 401. Los flujos de identidad son sincrónicos por
    naturaleza.
    """
    from hexcore.darwin.application import commands as modulo

    for nombre in modulo.__all__:
        objeto = getattr(modulo, nombre)
        if isinstance(objeto, type):
            assert not getattr(objeto, "__cqrs_background__", False), nombre


@pytest.mark.anyio
async def test_el_handler_usa_el_servicio_del_contenedor():
    """El handler resuelve del contenedor si no se le inyecta nada."""
    from hexcore.darwin.application.commands import SignUpHandler

    contenedor = configure_identity(_config(), **_componentes())
    handler = SignUpHandler()

    assert handler.service is contenedor.identity_service()


@pytest.mark.anyio
async def test_el_servicio_del_handler_se_puede_inyectar():
    """Para test: sin esto, probar un handler exigiría configurar el contenedor global."""
    from hexcore.darwin.application.commands import SignUpHandler

    centinela = object()
    handler = SignUpHandler(service=centinela)  # type: ignore[arg-type]

    assert handler.service is centinela
