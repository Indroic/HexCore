"""
Darwin: los eventos de identidad llevan la IP y el navegador del cliente.

Antes, `UserSignedInEvent` y compañía sólo traían actor, sujeto y transporte: un handler de
alertas o de auditoría tenía que reconstruir la IP fuera del framework. Lo que se fija acá:

1. **De dónde sale la IP.** Sin `trusted_proxies`, del socket, y `X-Forwarded-For` se ignora —lo
   escribe el cliente—. Con proxies declarados, sólo se lee si el par TCP inmediato es uno de
   ellos, y se toma la última IP que no sea de confianza (de derecha a izquierda).
2. **Cada evento listado la lleva**: sign-in (ok y fallido), sesión creada, refresh, reuso de
   refresh e impersonación (inicio y fin).
3. **La sesión rotada conserva el navegador y avanza `last_seen_at`.**
4. **El límite de sign-in usa esa misma IP**, así que detrás de un proxy deja de ser un cubo global.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus  # noqa: E402
from hexcore.darwin import (  # noqa: E402
    FixedClock,
    IdentityConfig,
    SessionCreatedEvent,
    SessionReuseDetectedEvent,
    StaticKeyStore,
    TokenConfig,
    UserSignedInEvent,
    UserSignInFailedEvent,
    build_identity_router,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    reset_identity,
)
from hexcore.darwin.domain.events import (  # noqa: E402
    ImpersonationEndedEvent,
    ImpersonationStartedEvent,
    SessionRefreshedEvent,
)
from hexcore.darwin.infrastructure.client_info import (  # noqa: E402
    MAX_USER_AGENT_LENGTH,
    client_info_from_request,
    resolve_client_ip,
)
from hexcore.darwin.infrastructure.transports import TRANSPORT_HEADER  # noqa: E402
from hexcore.fastapi import AppFeatures, create_app  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48
MAIL = "ana@test.com"
PASS = "una frase larga y buena"
NAVEGADOR = "Mozilla/5.0 (X11; Linux x86_64) Firefox/130.0"
OTRO_NAVEGADOR = "curl/8.5.0"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _peticion(par: str | None, **headers: str):
    """Lo mínimo que `resolve_client_ip` lee de un `Request`: `.client` y `.headers`."""
    return SimpleNamespace(
        client=SimpleNamespace(host=par) if par else None,
        headers={k.replace("_", "-"): v for k, v in headers.items()},
    )


# ── 1. De dónde sale la IP ────────────────────────────────────────────────────
class TestResolverLaIp:
    def test_sin_proxies_es_la_del_socket_y_se_ignora_la_cabecera(self):
        peticion = _peticion("203.0.113.9", X_Forwarded_For="1.2.3.4")

        assert resolve_client_ip(peticion) == "203.0.113.9"

    def test_un_par_que_no_es_proxy_no_puede_elegir_su_ip(self):
        """Alguien que llega directo con la cabecera puesta no se vuelve otro cliente."""
        peticion = _peticion("203.0.113.9", X_Forwarded_For="1.2.3.4")

        assert resolve_client_ip(peticion, ["10.0.0.0/8"]) == "203.0.113.9"

    def test_con_el_par_en_la_lista_se_lee_la_cabecera(self):
        peticion = _peticion("10.0.0.5", X_Forwarded_For="198.51.100.7")

        assert resolve_client_ip(peticion, ["10.0.0.0/8"]) == "198.51.100.7"

    def test_se_toma_la_ultima_ip_que_no_es_de_confianza(self):
        """
        Cadena: cliente → CDN (198.51.100.20, de confianza) → balanceador (10.0.0.5, el par).
        La izquierda la puede inventar el cliente: lo que cuenta es lo que agregó nuestro lado.
        """
        peticion = _peticion(
            "10.0.0.5", X_Forwarded_For="6.6.6.6, 198.51.100.7, 198.51.100.20, 10.0.0.9"
        )

        assert (
            resolve_client_ip(peticion, ["10.0.0.0/8", "198.51.100.20"])
            == "198.51.100.7"
        )

    def test_un_valor_inventado_a_la_izquierda_no_gana(self):
        peticion = _peticion("10.0.0.5", X_Forwarded_For="6.6.6.6, 198.51.100.7")

        assert resolve_client_ip(peticion, ["10.0.0.0/8"]) == "198.51.100.7"

    @pytest.mark.parametrize(
        "cabecera",
        ["", "basura", "198.51.100.7, basura", "10.0.0.1, 10.0.0.2"],
    )
    def test_ante_la_duda_se_cae_a_la_ip_del_par(self, cabecera):
        """Cabecera ausente, mal formada o toda de proxies: el valor que el cliente no controla."""
        peticion = _peticion("10.0.0.5", X_Forwarded_For=cabecera)

        assert resolve_client_ip(peticion, ["10.0.0.0/8"]) == "10.0.0.5"

    def test_ipv6(self):
        peticion = _peticion("fd00::5", X_Forwarded_For="2001:db8::7")

        assert resolve_client_ip(peticion, ["fd00::/8"]) == "2001:db8::7"

    def test_sin_socket_no_hay_ip(self):
        assert resolve_client_ip(_peticion(None, X_Forwarded_For="1.2.3.4")) is None
        assert (
            resolve_client_ip(_peticion(None, X_Forwarded_For="1.2.3.4"), ["10.0.0.0/8"])
            is None
        )

    def test_el_user_agent_se_trunca_al_largo_de_la_columna(self):
        peticion = _peticion("203.0.113.9", User_Agent="x" * (MAX_USER_AGENT_LENGTH + 500))

        info = client_info_from_request(peticion)

        assert info.user_agent == "x" * MAX_USER_AGENT_LENGTH

    def test_sin_user_agent_es_none(self):
        assert client_info_from_request(_peticion("203.0.113.9")).user_agent is None


class TestConfig:
    def test_un_proxy_mal_escrito_falla_al_configurar(self):
        with pytest.raises(ValidationError, match="trusted_proxies"):
            IdentityConfig(secret_key=CLAVE, trusted_proxies=("no-soy-una-ip",))

    def test_acepta_ips_y_redes(self):
        config = IdentityConfig(
            secret_key=CLAVE, trusted_proxies=("10.0.0.0/8", "172.18.0.5", "fd00::/8")
        )

        assert config.trusted_proxies == ("10.0.0.0/8", "172.18.0.5", "fd00::/8")

    def test_por_defecto_no_se_confia_en_nadie(self):
        assert IdentityConfig(secret_key=CLAVE).trusted_proxies == ()


# ── 2. Los eventos, por HTTP ──────────────────────────────────────────────────
class _Captura:
    """Un bus en memoria al que se suscriben las clases exactas que interesan."""

    def __init__(self) -> None:
        self.bus = InMemoryEventBus()
        self.eventos: list[object] = []
        for clase in (
            UserSignedInEvent,
            UserSignInFailedEvent,
            SessionCreatedEvent,
            SessionRefreshedEvent,
            SessionReuseDetectedEvent,
            ImpersonationStartedEvent,
            ImpersonationEndedEvent,
        ):
            self.bus.subscribe(clase, self._guardar)

    async def _guardar(self, evento) -> None:
        self.eventos.append(evento)

    def de(self, clase):
        return [e for e in self.eventos if type(e) is clase]

    def unico(self, clase):
        encontrados = self.de(clase)
        assert len(encontrados) == 1, [type(e).__name__ for e in self.eventos]
        return encontrados[0]


def _configurar(*, captura: _Captura, reloj: FixedClock, **config):
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))

    # El `rate_limit` usa un `MemoryCache` global del proceso: sin resetearlo, los intentos se
    # acumulan entre tests.
    from hexcore.config import LazyConfig
    from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

    LazyConfig.get_config().cache_backend = MemoryCache()

    reset_identity()
    return configure_identity(
        IdentityConfig(
            storage="sqlalchemy",
            secret_key=CLAVE,
            tokens=TokenConfig(issuer="https://api.test"),
            require_verified_email=False,
            **config,
        ),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        events=captura.bus,
    )


@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def captura() -> _Captura:
    return _Captura()


@pytest.fixture
def app_factory(captura, reloj):
    """Fábrica de `(app, contenedor)` con la configuración de Darwin que pida cada test."""
    creados = []

    def fabricar(*, rate_limit=(100, 60), **config):
        contenedor = _configurar(captura=captura, reloj=reloj, **config)
        creados.append(contenedor)
        app = create_app(
            features=AppFeatures(auth_context=True, csrf=False, health=False),
            routers=[build_identity_router(sign_in_rate_limit=rate_limit)],
        )
        return app, contenedor

    yield fabricar
    reset_identity()
    asyncio.run(dispose_engine())


def _cliente(app, par: str = "203.0.113.9") -> TestClient:
    """Un `TestClient` cuyo socket dice ser `par`."""
    return TestClient(app, client=(par, 50000), raise_server_exceptions=False)


def _alta(contenedor) -> None:
    async def _crear():
        usuario, _ = await contenedor.identity_service().sign_up(
            email=MAIL, password=PASS
        )
        await contenedor.users().update(usuario.model_copy(update={"email_verified": True}))

    asyncio.run(_crear())


def _sign_in(cliente: TestClient, *, password: str = PASS, **headers: str):
    return cliente.post(
        "/auth/sign-in",
        json={"email": MAIL, "password": password},
        headers={TRANSPORT_HEADER: "bearer", **headers},
    )


def _refresh(cliente: TestClient, refresh_token: str, **headers: str):
    return cliente.post(
        "/auth/refresh",
        headers={TRANSPORT_HEADER: "bearer", "X-Refresh-Token": refresh_token, **headers},
    )


class TestEventosDeSignIn:
    def test_el_sign_in_lleva_la_ip_del_socket_y_el_navegador(self, app_factory, captura):
        app, contenedor = app_factory()
        _alta(contenedor)

        respuesta = _sign_in(_cliente(app), **{"User-Agent": NAVEGADOR})

        assert respuesta.status_code == 200, respuesta.text
        entro = captura.unico(UserSignedInEvent)
        assert (entro.client_ip, entro.user_agent) == ("203.0.113.9", NAVEGADOR)

    def test_la_sesion_creada_lleva_la_ip_y_el_navegador(self, app_factory, captura):
        app, contenedor = app_factory()
        _alta(contenedor)

        _sign_in(_cliente(app), **{"User-Agent": NAVEGADOR})

        creada = captura.unico(SessionCreatedEvent)
        assert (creada.client_ip, creada.user_agent) == ("203.0.113.9", NAVEGADOR)

    @pytest.mark.parametrize("motivo", ["bad-password", "unknown-account"])
    def test_el_sign_in_fallido_lleva_la_ip_y_el_navegador(
        self, app_factory, captura, motivo
    ):
        app, contenedor = app_factory()
        _alta(contenedor)
        cliente = _cliente(app)

        if motivo == "bad-password":
            respuesta = _sign_in(cliente, password="otra clave", **{"User-Agent": NAVEGADOR})
        else:
            respuesta = cliente.post(
                "/auth/sign-in",
                json={"email": "nadie@test.com", "password": PASS},
                headers={TRANSPORT_HEADER: "bearer", "User-Agent": NAVEGADOR},
            )

        assert respuesta.status_code == 401
        fallo = captura.unico(UserSignInFailedEvent)
        assert fallo.reason == motivo
        assert (fallo.client_ip, fallo.user_agent) == ("203.0.113.9", NAVEGADOR)

    def test_sin_proxy_de_confianza_la_cabecera_no_cambia_la_ip(self, app_factory, captura):
        app, contenedor = app_factory()
        _alta(contenedor)

        _sign_in(_cliente(app), **{"X-Forwarded-For": "1.2.3.4"})

        assert captura.unico(UserSignedInEvent).client_ip == "203.0.113.9"

    def test_con_proxy_de_confianza_es_la_ip_de_la_cabecera(self, app_factory, captura):
        app, contenedor = app_factory(trusted_proxies=("10.0.0.0/8",))
        _alta(contenedor)

        _sign_in(
            _cliente(app, par="10.0.0.5"),
            **{"X-Forwarded-For": "6.6.6.6, 198.51.100.7"},
        )

        assert captura.unico(UserSignedInEvent).client_ip == "198.51.100.7"
        assert captura.unico(SessionCreatedEvent).client_ip == "198.51.100.7"

    def test_con_proxies_declarados_un_par_ajeno_sigue_sin_poder_elegir(
        self, app_factory, captura
    ):
        app, contenedor = app_factory(trusted_proxies=("10.0.0.0/8",))
        _alta(contenedor)

        _sign_in(_cliente(app, par="203.0.113.9"), **{"X-Forwarded-For": "1.2.3.4"})

        assert captura.unico(UserSignedInEvent).client_ip == "203.0.113.9"

    def test_la_sesion_guarda_la_misma_ip_que_el_evento(self, app_factory, captura):
        app, contenedor = app_factory(trusted_proxies=("10.0.0.0/8",))
        _alta(contenedor)

        respuesta = _sign_in(
            _cliente(app, par="10.0.0.5"),
            **{"X-Forwarded-For": "198.51.100.7", "User-Agent": NAVEGADOR},
        )
        sesion_id = captura.unico(SessionCreatedEvent).session_id

        sesion = asyncio.run(contenedor.sessions_repository().get(sesion_id))
        assert respuesta.status_code == 200
        assert (sesion.ip_address, sesion.user_agent) == ("198.51.100.7", NAVEGADOR)


# ── 3. Refresh y reuso ────────────────────────────────────────────────────────
class TestRefresh:
    def _entrar(self, app, contenedor, **headers):
        _alta(contenedor)
        respuesta = _sign_in(_cliente(app), **{"User-Agent": NAVEGADOR, **headers})
        assert respuesta.status_code == 200, respuesta.text
        return respuesta.json()["refresh_token"]

    def test_la_sesion_rotada_conserva_el_navegador_y_actualiza_last_seen_at(
        self, app_factory, captura, reloj
    ):
        app, contenedor = app_factory()
        refresh = self._entrar(app, contenedor)
        inicial = asyncio.run(
            contenedor.sessions_repository().get(captura.unico(SessionCreatedEvent).session_id)
        )
        assert inicial.last_seen_at == AHORA

        reloj.advance(seconds=300)
        respuesta = _refresh(_cliente(app), refresh, **{"User-Agent": OTRO_NAVEGADOR})

        assert respuesta.status_code == 200, respuesta.text
        rotada = asyncio.run(
            contenedor.sessions_repository().get(captura.unico(SessionRefreshedEvent).session_id)
        )
        # La fila de origen: el navegador del sign-in, no el del refresh.
        assert rotada.user_agent == NAVEGADOR
        assert rotada.ip_address == "203.0.113.9"
        assert rotada.last_seen_at == AHORA.replace(minute=5)

    def test_el_evento_de_refresh_lleva_los_datos_de_esa_peticion(
        self, app_factory, captura, reloj
    ):
        app, contenedor = app_factory()
        refresh = self._entrar(app, contenedor)

        reloj.advance(seconds=30)
        _refresh(_cliente(app, par="198.51.100.20"), refresh, **{"User-Agent": OTRO_NAVEGADOR})

        evento = captura.unico(SessionRefreshedEvent)
        assert (evento.client_ip, evento.user_agent) == ("198.51.100.20", OTRO_NAVEGADOR)

    def test_el_reuso_lleva_la_ip_y_el_navegador_de_quien_presento_el_token(
        self, app_factory, captura, reloj
    ):
        """El dato útil de una alerta de robo: no el dueño de la sesión, sino quien la reusó."""
        app, contenedor = app_factory()
        refresh = self._entrar(app, contenedor)
        assert _refresh(_cliente(app), refresh).status_code == 200

        reloj.advance(seconds=60)  # fuera de la ventana de gracia
        respuesta = _refresh(
            _cliente(app, par="198.51.100.66"), refresh, **{"User-Agent": OTRO_NAVEGADOR}
        )

        assert respuesta.status_code == 401
        reuso = captura.unico(SessionReuseDetectedEvent)
        assert (reuso.client_ip, reuso.user_agent) == ("198.51.100.66", OTRO_NAVEGADOR)

    def test_mis_sesiones_expone_last_seen_at(self, app_factory, captura):
        app, contenedor = app_factory()
        _alta(contenedor)
        cliente = _cliente(app)
        token = _sign_in(cliente).json()["access_token"]

        r = cliente.get(
            "/auth/sessions",
            headers={TRANSPORT_HEADER: "bearer", "Authorization": f"Bearer {token}"},
        )

        assert r.status_code == 200, r.text
        assert r.json()[0]["last_seen_at"] == AHORA.isoformat()


# ── 4. El límite de intentos usa la misma IP ──────────────────────────────────
class TestLimiteDeIntentos:
    def test_detras_de_un_proxy_declarado_cada_cliente_tiene_su_cubo(self, app_factory):
        app, contenedor = app_factory(rate_limit=(2, 60), trusted_proxies=("10.0.0.0/8",))
        _alta(contenedor)
        proxy = _cliente(app, par="10.0.0.5")

        for _ in range(2):
            assert _sign_in(proxy, **{"X-Forwarded-For": "198.51.100.1"}).status_code == 200
        bloqueado = _sign_in(proxy, **{"X-Forwarded-For": "198.51.100.1"})
        otro_cliente = _sign_in(proxy, **{"X-Forwarded-For": "198.51.100.2"})

        assert bloqueado.status_code == 429
        assert otro_cliente.status_code == 200

    def test_sin_proxy_declarado_la_cabecera_no_da_un_cubo_nuevo(self, app_factory):
        """Rotar `X-Forwarded-For` en cada petición no debe esquivar el límite."""
        app, contenedor = app_factory(rate_limit=(2, 60))
        _alta(contenedor)
        cliente = _cliente(app)

        estados = [
            _sign_in(cliente, **{"X-Forwarded-For": f"198.51.100.{n}"}).status_code
            for n in range(1, 4)
        ]

        assert estados == [200, 200, 429]


# ── 5. Impersonación ──────────────────────────────────────────────────────────
class TestImpersonacion:
    @pytest.fixture
    def montaje(self, captura, reloj):
        from hexcore.darwin import PluginRegistry
        from hexcore.darwin.plugins.impersonate import (
            IMPERSONATE_SCOPE,
            ImpersonatePlugin,
            ScopeImpersonationPolicy,
        )

        plugin = ImpersonatePlugin(policy=ScopeImpersonationPolicy())
        asyncio.run(dispose_engine())
        motor = init_engine(SQLITE_URL, poolclass=StaticPool)
        asyncio.run(create_identity_tables(motor))

        from hexcore.config import LazyConfig
        from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

        LazyConfig.get_config().cache_backend = MemoryCache()

        reset_identity()
        plugin.reset()
        contenedor = configure_identity(
            IdentityConfig(
                storage="sqlalchemy",
                secret_key=CLAVE,
                tokens=TokenConfig(issuer="https://api.test"),
                require_verified_email=False,
                trusted_proxies=("10.0.0.0/8",),
            ),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            events=captura.bus,
            plugins=PluginRegistry([plugin]),
        )
        app = create_app(
            features=AppFeatures(auth_context=True, csrf=False, health=False),
            routers=[build_identity_router(), *plugin.routers()],
        )
        yield app, contenedor, IMPERSONATE_SCOPE
        reset_identity()
        plugin.reset()
        asyncio.run(dispose_engine())

    def test_empezar_y_terminar_llevan_la_ip_y_el_navegador(self, montaje, captura):
        app, contenedor, scope = montaje

        async def _preparar():
            operador, _ = await contenedor.identity_service().sign_up(
                email="soporte@ejemplo.com", password=PASS
            )
            await contenedor.users().update(
                operador.model_copy(update={"email_verified": True})
            )
            cliente, _ = await contenedor.identity_service().sign_up(
                email="cliente@ejemplo.com", password=PASS
            )
            await contenedor.users().update(
                cliente.model_copy(update={"email_verified": True})
            )
            _, _, par = await contenedor.identity_service().sign_in(
                identifier="soporte@ejemplo.com",
                password=PASS,
                transport="bearer",
                scopes=(scope,),
            )
            return operador, cliente, par

        operador, cliente, par = asyncio.run(_preparar())
        http = _cliente(app, par="10.0.0.5")
        auth = {
            "Authorization": f"Bearer {par.access_token}",
            TRANSPORT_HEADER: "bearer",
            "X-Forwarded-For": "198.51.100.7",
            "User-Agent": NAVEGADOR,
        }

        inicio = http.post(
            f"/auth/impersonate/{cliente.id}", json={"reason": "ticket #4821"}, headers=auth
        )
        assert inicio.status_code == 200, inicio.text
        empezo = captura.unico(ImpersonationStartedEvent)
        assert (empezo.client_ip, empezo.user_agent) == ("198.51.100.7", NAVEGADOR)
        assert empezo.actor_user_id == operador.id
        assert empezo.subject_user_id == cliente.id
        assert empezo.impersonated is True
        assert empezo.reason == "ticket #4821"

        como_cliente = {**auth, "Authorization": f"Bearer {inicio.json()['access_token']}"}
        fin = http.post("/auth/impersonate/stop", headers=como_cliente)
        assert fin.status_code == 200, fin.text
        termino = captura.unico(ImpersonationEndedEvent)
        assert (termino.client_ip, termino.user_agent) == ("198.51.100.7", NAVEGADOR)
        assert termino.ended_by == "manual"
        assert termino.actor_user_id == operador.id
        assert termino.session_id == empezo.session_id
