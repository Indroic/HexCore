"""
Darwin Fase 8: `magic_link`, el plugin de referencia, contra SQLite y la app real.

El plugin existe para demostrar que un flujo de auth completo se escribe **sin tocar el
núcleo**: reusa la tabla `verification`, el `session_service`, el transporte dual y el
`emit_tokens` del router de identidad. Si algo de eso hubiera que duplicarlo, el sistema de
plugins no estaría terminado.

Lo adversarial que se fija:

- **No es un oráculo de enumeración**: la respuesta es idéntica exista o no la cuenta.
- **Un solo uso, bajo concurrencia**: dos canjes simultáneos del mismo token, exactamente uno
  gana. Es la parte que un test secuencial no probaría.
- **Pedir de nuevo invalida el anterior**: cinco clicks en "reenviar" no dejan cinco links
  válidos.
- **Vencido es inválido**, con el mismo error que uno falso.
- **Verifica el mail como efecto**, sin pedir un segundo mail por algo que acaba de pasar.
- **Rate limit en la ruta pública**, que sin él es un amplificador de mail contra terceros.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    FixedClock,
    IdentityConfig,
    InvalidCredentialsError,
    StaticKeyStore,
    TokenConfig,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    reset_identity,
)
from hexcore.darwin.plugins.magic_link import (  # noqa: E402
    DEFAULT_TTL,
    MAGIC_LINK_PURPOSE,
    MagicLinkPlugin,
)
from hexcore.darwin.plugins.magic_link.commands import (  # noqa: E402
    ConsumeMagicLink,
    ConsumeMagicLinkHandler,
    RequestMagicLink,
    RequestMagicLinkHandler,
    consume_magic_link,
    request_magic_link,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48
MAIL = "ana@ejemplo.com"
PASS = "una frase larga y buena"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def contenedor(reloj):
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))

    from hexcore.config import LazyConfig
    from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

    # Mismo motivo que en `test_darwin_http.py`: el `rate_limit` del router usa el
    # `MemoryCache` global del proceso, y sin resetearlo el contador se acumula entre tests.
    LazyConfig.get_config().cache_backend = MemoryCache()

    reset_identity()
    contenedor = configure_identity(
        IdentityConfig(
            secret_key=CLAVE,
            tokens=TokenConfig(issuer="https://api.test"),
            require_verified_email=False,
        ),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
    )
    yield contenedor

    reset_identity()
    asyncio.run(dispose_engine())


async def _fila(hash_: str):
    """
    Lee la fila de `verification` directo por SQL.

    El repositorio no expone un `find`, y no debería: leer un token por su hash no es una
    operación del dominio — el único camino legítimo es canjearlo. Acá se hace por SQL crudo
    justamente para poder aseverar lo que el dominio no deja preguntar.
    """
    from sqlalchemy import select

    from hexcore.darwin.infrastructure.models import VerificationModel
    from hexcore.infrastructure.uow.scopes import session_scope

    async with session_scope() as sesion:
        resultado = await sesion.execute(
            select(VerificationModel).where(VerificationModel.value_hash == hash_)
        )
        return resultado.scalar_one_or_none()


async def _alta(contenedor, *, email: str = MAIL, verificado: bool = True):
    usuario, _ = await contenedor.identity_service().sign_up(email=email, password=PASS)
    if verificado:
        usuario = await contenedor.users().update(
            usuario.model_copy(update={"email_verified": True})
        )
    return usuario


# ── El flujo feliz ────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_pedir_y_canjear_abre_sesion(contenedor):
    usuario = await _alta(contenedor)

    emitido = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    assert emitido.token is not None

    resultado = await consume_magic_link(email=MAIL, token=emitido.token)

    assert resultado.user.id == usuario.id
    assert resultado.session.actor_user_id == usuario.id
    assert resultado.session.subject_user_id == usuario.id
    assert resultado.tokens.access_token
    assert resultado.tokens.refresh_token


@pytest.mark.anyio
async def test_el_token_emitido_verifica_y_trae_el_sid_de_la_sesion(contenedor):
    """El link no es un canal aparte: sale la misma sesión que un sign-in normal."""
    await _alta(contenedor)
    emitido = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    resultado = await consume_magic_link(email=MAIL, token=emitido.token or "")

    claims = await contenedor.verifier().verify(
        resultado.tokens.access_token, transport="cookie"
    )

    assert claims.sid == resultado.session.id
    assert claims.act == claims.sub, "sin impersonación, actor y subject coinciden"


@pytest.mark.anyio
async def test_el_mail_se_normaliza(contenedor):
    """Pedir con otra capitalización tiene que llegar a la misma cuenta."""
    await _alta(contenedor)

    emitido = await request_magic_link(email="ANA@Ejemplo.COM", ttl=DEFAULT_TTL)

    assert emitido.email == MAIL
    assert emitido.token is not None
    resultado = await consume_magic_link(email="Ana@Ejemplo.com", token=emitido.token)
    assert resultado.user.email == MAIL


# ── El token no se guarda en claro ────────────────────────────────────────────
@pytest.mark.anyio
async def test_la_fila_guarda_el_hash_y_no_el_token(contenedor):
    """
    Una base filtrada no puede convertirse en un login: el token viaja por mail y la fila
    guarda su hash, igual que el refresh.
    """
    from hexcore.darwin.infrastructure.hashing import hash_token

    await _alta(contenedor)
    emitido = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    assert emitido.token is not None

    fila = await _fila(hash_token(emitido.token))

    assert fila is not None, "la fila se guarda bajo el hash, no bajo el token"
    assert fila.purpose == MAGIC_LINK_PURPOSE
    assert await _fila(emitido.token) is None, "el token en claro no está en ninguna fila"


# ── Enumeración ───────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_una_cuenta_inexistente_no_lanza_ni_deja_rastro(contenedor):
    """
    Devolver un error diría que el mail no está registrado, en una ruta pública y sin
    autenticación. `token=None` deja que el llamador responda lo mismo en los dos casos.
    """
    emitido = await request_magic_link(email="nadie@ejemplo.com", ttl=DEFAULT_TTL)

    assert emitido.token is None
    assert emitido.email == "nadie@ejemplo.com"


@pytest.mark.anyio
async def test_canjear_un_token_inventado_da_el_error_generico(contenedor):
    await _alta(contenedor)

    with pytest.raises(InvalidCredentialsError):
        await consume_magic_link(email=MAIL, token="inventado")


@pytest.mark.anyio
async def test_el_token_de_otro_mail_no_sirve(contenedor):
    """
    El `consume` filtra por `(identifier, purpose, hash)`: un token válido apuntado a otra
    cuenta no la abre.
    """
    await _alta(contenedor)
    await _alta(contenedor, email="otro@ejemplo.com")
    emitido = await request_magic_link(email="otro@ejemplo.com", ttl=DEFAULT_TTL)

    with pytest.raises(InvalidCredentialsError):
        await consume_magic_link(email=MAIL, token=emitido.token or "")


# ── Un solo uso ───────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_el_link_es_de_un_solo_uso(contenedor):
    await _alta(contenedor)
    emitido = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    assert emitido.token is not None

    await consume_magic_link(email=MAIL, token=emitido.token)

    with pytest.raises(InvalidCredentialsError):
        await consume_magic_link(email=MAIL, token=emitido.token)


@pytest.mark.anyio
async def test_el_canje_estampa_consumed_at(contenedor):
    from hexcore.darwin.infrastructure.hashing import hash_token

    await _alta(contenedor)
    emitido = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    assert emitido.token is not None
    hash_ = hash_token(emitido.token)

    antes = await _fila(hash_)
    assert antes is not None and antes.consumed_at is None

    await consume_magic_link(email=MAIL, token=emitido.token)

    despues = await _fila(hash_)
    assert despues is not None and despues.consumed_at is not None


@pytest.mark.anyio
async def test_dos_canjes_concurrentes_dejan_pasar_exactamente_uno(contenedor):
    """
    Es la razón por la que `consume` es un `UPDATE ... WHERE consumed_at IS NULL RETURNING` y
    no un SELECT seguido de un UPDATE: con el par leer-escribir, dos clicks simultáneos —el
    doble click del usuario, o el prefetch del cliente de mail— abrirían **dos** sesiones con
    el mismo link, y "de un solo uso" sería falso justo cuando importa.
    """
    await _alta(contenedor)
    emitido = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    assert emitido.token is not None

    resultados = await asyncio.gather(
        *(
            consume_magic_link(email=MAIL, token=emitido.token)
            for _ in range(8)
        ),
        return_exceptions=True,
    )

    ganaron = [r for r in resultados if not isinstance(r, BaseException)]
    perdieron = [r for r in resultados if isinstance(r, InvalidCredentialsError)]

    assert len(ganaron) == 1, f"ganaron {len(ganaron)}, tenía que ganar uno"
    assert len(perdieron) == 7
    assert len(ganaron) + len(perdieron) == 8, "ninguna falló por otra cosa"


@pytest.mark.anyio
async def test_pedir_de_nuevo_invalida_el_anterior(contenedor):
    """
    Sin esto, cinco clicks en "reenviar" dejan cinco links válidos y el espacio a adivinar se
    multiplica por cinco. Además, el usuario que pide uno nuevo porque no le llegó el primero
    espera que el primero deje de servir.
    """
    await _alta(contenedor)
    primero = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    segundo = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    assert primero.token is not None and segundo.token is not None
    assert primero.token != segundo.token

    with pytest.raises(InvalidCredentialsError):
        await consume_magic_link(email=MAIL, token=primero.token)

    resultado = await consume_magic_link(email=MAIL, token=segundo.token)
    assert resultado.tokens.access_token


# ── Vencimiento ───────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_un_link_vencido_da_el_error_generico(contenedor, reloj):
    """
    El mismo error que un token falso: distinguirlos diría que el mail tiene un link pendiente,
    o sea que la cuenta existe.
    """
    await _alta(contenedor)
    emitido = await request_magic_link(email=MAIL, ttl=timedelta(minutes=15))
    assert emitido.token is not None

    reloj.advance(minutes=16)

    with pytest.raises(InvalidCredentialsError):
        await consume_magic_link(email=MAIL, token=emitido.token)


@pytest.mark.anyio
async def test_justo_antes_de_vencer_todavia_sirve(contenedor, reloj):
    await _alta(contenedor)
    emitido = await request_magic_link(email=MAIL, ttl=timedelta(minutes=15))
    assert emitido.token is not None

    reloj.advance(minutes=14, seconds=59)

    resultado = await consume_magic_link(email=MAIL, token=emitido.token)
    assert resultado.tokens.access_token


# ── El mail queda verificado ──────────────────────────────────────────────────
@pytest.mark.anyio
async def test_el_canje_verifica_el_mail(contenedor):
    """
    Quien probó que controla la casilla ya demostró lo que la verificación de mail prueba.
    Dejarlo sin verificar obligaría a un segundo mail por algo que acaba de ocurrir.
    """
    usuario = await _alta(contenedor, verificado=False)
    assert usuario.email_verified is False

    emitido = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)
    resultado = await consume_magic_link(email=MAIL, token=emitido.token or "")

    assert resultado.user.email_verified is True
    persistido = await contenedor.users().get_by_email(MAIL)
    assert persistido is not None and persistido.email_verified is True


@pytest.mark.anyio
async def test_una_cuenta_bloqueada_no_entra_por_el_link(contenedor, reloj):
    """
    Si el link esquivara el bloqueo, sería una puerta lateral alrededor del techo de intentos
    de contraseña.
    """
    from hexcore.darwin import AccountLockedError

    usuario = await _alta(contenedor)
    await contenedor.users().update(
        usuario.model_copy(
            update={"locked_until": AHORA + timedelta(minutes=30)}
        )
    )
    emitido = await request_magic_link(email=MAIL, ttl=DEFAULT_TTL)

    with pytest.raises(AccountLockedError):
        await consume_magic_link(email=MAIL, token=emitido.token or "")


# ── Los handlers CQRS ─────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_los_handlers_despachan_el_mismo_flujo(contenedor):
    """
    El plugin expone las dos formas: llamar el servicio o despachar el comando. La segunda es
    la que pasa por el pipeline, y por lo tanto por los hooks.
    """
    await _alta(contenedor)

    emitido = await RequestMagicLinkHandler().handle(RequestMagicLink(email=MAIL))
    assert emitido.token is not None

    resultado = await ConsumeMagicLinkHandler().handle(
        ConsumeMagicLink(email=MAIL, token=emitido.token, transport="bearer")
    )

    assert resultado.session.transport == "bearer"


@pytest.mark.anyio
async def test_los_comandos_declaran_su_accion():
    """Es el contrato al que se enganchan los hooks de otros plugins."""
    from hexcore.darwin import action_of

    assert action_of(RequestMagicLink) == "magic_link.request"
    assert action_of(ConsumeMagicLink) == "magic_link.consume"


@pytest.mark.anyio
async def test_un_hook_del_plugin_corre_en_el_pipeline(contenedor):
    """
    El plugin no necesita nada nuevo para engancharse: `HookMiddleware` es un
    `AbstractMiddleware` y el comando ya declara su acción.
    """
    from hexcore.application.cqrs.in_memory_buses import InMemoryCommandBus
    from hexcore.application.cqrs.pipeline import MiddlewarePipeline
    from hexcore.darwin import (
        DarwinPlugin,
        HookBinding,
        HookMiddleware,
        PluginRegistry,
    )
    from hexcore.testing import build_test_buses

    vistos: list[str] = []

    async def observar(payload: object) -> None:
        vistos.append(type(payload).__name__)
        return None

    class Espia(DarwinPlugin):
        name = "espia"
        requires = ("magic_link",)

        def hooks(self):
            return [
                HookBinding(action="magic_link.*", phase="before", handler=observar)
            ]

    registro = PluginRegistry([MagicLinkPlugin(), Espia()])
    await _alta(contenedor)

    buses = build_test_buses()
    buses.registry.register_command_handler(RequestMagicLink, RequestMagicLinkHandler())
    bus = InMemoryCommandBus(
        registry=buses.registry,
        pipeline=MiddlewarePipeline([HookMiddleware(registro)]),
    )

    emitido = await bus.dispatch(RequestMagicLink(email=MAIL))

    assert emitido.token is not None
    assert vistos == ["RequestMagicLink"]


# ── El plugin como plugin ─────────────────────────────────────────────────────
def test_el_plugin_no_aporta_tablas():
    """
    Reusa `verification` en vez de contribuir una tabla propia: un magic link **es** una
    verificación de un solo uso con vencimiento, y una tabla nueva obligaría al consumidor a
    una migración por un flujo que el esquema ya modela.
    """
    plugin = MagicLinkPlugin()

    assert plugin.tables() == {}
    assert plugin.name == "magic_link"


def test_el_plugin_aporta_su_router():
    pytest.importorskip("fastapi")
    plugin = MagicLinkPlugin()

    assert len(plugin.routers()) == 1


def test_el_plugin_registra_sus_handlers():
    from hexcore.application.cqrs.registry import HandlerRegistry

    registro = HandlerRegistry()
    MagicLinkPlugin().register_handlers(registro)

    assert registro.resolve_command_handler(RequestMagicLink) is not None
    assert registro.resolve_command_handler(ConsumeMagicLink) is not None


def test_el_plugin_valida_en_un_registro():
    from hexcore.darwin import PluginRegistry

    registro = PluginRegistry([MagicLinkPlugin()])
    registro.validate()

    assert registro.names == ("magic_link",)


# ── El borde HTTP ─────────────────────────────────────────────────────────────
@pytest.fixture
def cliente(contenedor):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from hexcore.darwin import build_identity_router
    from hexcore.darwin.plugins.magic_link.router import build_magic_link_router
    from hexcore.fastapi import AppFeatures, create_app

    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True, health=False),
        routers=[build_identity_router(), build_magic_link_router()],
    )
    with TestClient(app) as cliente:
        yield cliente


@pytest.mark.anyio
async def test_http_pedir_y_canjear(contenedor, cliente):
    await _alta(contenedor)

    pedido = cliente.post("/auth/magic-link/request", json={"email": MAIL})
    assert pedido.status_code == 200
    assert pedido.json()["sent"] is True
    token = pedido.json()["token"]

    canje = cliente.post(
        "/auth/magic-link/consume",
        json={"email": MAIL, "token": token},
        headers={"X-Darwin-Transport": "bearer"},
    )

    assert canje.status_code == 200
    assert canje.json()["access_token"]
    assert not canje.headers.get_list("set-cookie"), "en Bearer no se setean cookies"


@pytest.mark.anyio
async def test_http_la_respuesta_es_identica_exista_o_no_la_cuenta(contenedor, cliente):
    """
    Lo único que cambia es que uno trae `token` — y en producción ese campo no se devuelve, o
    sea que las dos respuestas son byte a byte iguales.
    """
    await _alta(contenedor)

    existe = cliente.post("/auth/magic-link/request", json={"email": MAIL})
    no_existe = cliente.post(
        "/auth/magic-link/request", json={"email": "nadie@ejemplo.com"}
    )

    assert existe.status_code == no_existe.status_code == 200
    assert no_existe.json() == {"sent": True}
    assert existe.json()["sent"] is True


@pytest.mark.anyio
async def test_http_un_token_falso_da_401(contenedor, cliente):
    await _alta(contenedor)

    respuesta = cliente.post(
        "/auth/magic-link/consume",
        json={"email": MAIL, "token": "inventado"},
        headers={"X-Darwin-Transport": "bearer"},
    )

    assert respuesta.status_code == 401
    assert "WWW-Authenticate" in respuesta.headers


@pytest.mark.anyio
async def test_http_el_rate_limit_frena_el_cuarto_pedido(contenedor, cliente):
    """
    La ruta es pública y manda mails: sin techo es un amplificador de mail gratuito contra un
    tercero que nunca pidió estar en el sistema. El default es `(3, 900)`.
    """
    await _alta(contenedor)

    codigos = [
        cliente.post("/auth/magic-link/request", json={"email": MAIL}).status_code
        for _ in range(4)
    ]

    assert codigos[:3] == [200, 200, 200]
    assert codigos[3] == 429


@pytest.mark.anyio
async def test_http_el_canje_por_cookie_setea_la_cookie(contenedor, cliente):
    await _alta(contenedor)
    token = cliente.post("/auth/magic-link/request", json={"email": MAIL}).json()["token"]

    canje = cliente.post("/auth/magic-link/consume", json={"email": MAIL, "token": token})

    assert canje.status_code == 200
    cookies = " ".join(canje.headers.get_list("set-cookie"))
    assert "HttpOnly" in cookies
    assert "access_token" not in canje.json(), "en cookie los tokens no van en el cuerpo"
