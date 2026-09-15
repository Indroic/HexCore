"""
Darwin Fase 7: el borde HTTP, contra la app real.

`TestClient` sobre `create_app()`, con SQLite en memoria. Lo que se fija:

1. **Un endpoint, dos transportes.** El cliente cookie recibe `Set-Cookie` y **ningún** token
   en el cuerpo; el cliente Bearer recibe los tokens en el cuerpo y **ningún** `Set-Cookie`.
2. **Los atributos de la cookie, aserteados literalmente**: prefijo `__Host-`, `HttpOnly`,
   `Secure`, `SameSite=Lax`, `Path=/`, sin `Domain`.
3. **El token va atado a su transporte**: una cookie replayeada como Bearer se rechaza.
4. **CSRF**: cross-origin con cookie válida → 403; origen declarado + double-submit → 200; sin
   header → 403; valor forjado → 403; `GET` exento.
5. **401 con `WWW-Authenticate`** (RFC 6750 §3), que sólo es posible por el `headers_for` de
   la Fase 0.
6. **Fijación de sesión**: el token cambia en sign-in y en cambio de contraseña.
7. El `reset` del ContextVar ocurre **aunque el endpoint lance**.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from fastapi import Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    AuthContext,
    CSRF_HEADER,
    CookieConfig,
    FixedClock,
    IdentityConfig,
    StaticKeyStore,
    TokenConfig,
    build_identity_router,
    configure_identity,
    create_identity_tables,
    current_auth,
    derive_csrf_token,
    generate_signing_key,
    provide_auth,
    require_scopes,
    reset_identity,
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
ORIGEN = "https://app.test"
MAIL = "ana@test.com"
PASS = "una frase larga y buena"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _configurar(*, secure_cookies: bool = False, verified: bool = False):
    """
    Cablea Darwin contra SQLite en memoria.

    `secure=False` por defecto: `TestClient` habla `http://`, y una cookie `Secure` no se
    guardaría — el test fallaría por el transporte y no por lo que quiere probar. Los tests
    que verifican los atributos de la cookie sí piden `secure=True` y leen el header crudo.
    """
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))

    # El `rate_limit` del router usa `config.cache_backend`, que es un `MemoryCache`
    # **global del proceso**. Sin resetearlo, el contador de intentos de login se acumula
    # entre tests: los primeros pasan y del sexto en adelante todo da 429, con el síntoma
    # desconcertante de que cada test pasa aislado y falla en la suite.
    from hexcore.config import LazyConfig
    from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

    LazyConfig.get_config().cache_backend = MemoryCache()

    reset_identity()
    return configure_identity(
        IdentityConfig(
            storage="sqlalchemy",
            secret_key=CLAVE,
            tokens=TokenConfig(issuer="https://api.test"),
            cookies=CookieConfig(secure=secure_cookies),
            trusted_origins=(ORIGEN,),
            require_verified_email=not verified,
        ),
        clock=FixedClock(AHORA),
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
    )


@pytest.fixture
def contenedor():
    yield _configurar(verified=True)
    reset_identity()
    asyncio.run(dispose_engine())


@pytest.fixture
def app(contenedor):
    aplicacion = create_app(
        features=AppFeatures(auth_context=True, csrf=True, health=False),
        routers=[build_identity_router()],
    )

    @aplicacion.get("/protegida")
    async def protegida(auth: AuthContext = Depends(provide_auth)) -> dict[str, str]:
        return {"actor": str(auth.actor_id), "transport": auth.transport}

    @aplicacion.get("/publica")
    async def publica() -> dict[str, bool]:
        return {"ok": True, "hay_contexto": current_auth() is not None}

    @aplicacion.get("/con-scope", dependencies=[Depends(require_scopes("dinero.mover"))])
    async def con_scope() -> dict[str, bool]:
        return {"ok": True}

    @aplicacion.get("/explota")
    async def explota(auth: AuthContext = Depends(provide_auth)) -> None:
        raise RuntimeError("el endpoint falló con contexto activo")

    return aplicacion


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _alta(client: TestClient) -> None:
    """Crea la cuenta y verifica el mail, dejándola lista para el sign-in."""
    r = client.post("/auth/sign-up", json={"email": MAIL, "password": PASS})
    assert r.status_code == 201, r.text
    codigo = r.json()["verification_code"]
    r = client.post("/auth/verify-email", json={"email": MAIL, "code": codigo})
    assert r.status_code == 200, r.text


def _sign_in_cookie(client: TestClient):
    return client.post(
        "/auth/sign-in",
        json={"email": MAIL, "password": PASS},
        headers={"Origin": ORIGEN},
    )


def _sign_in_bearer(client: TestClient):
    return client.post(
        "/auth/sign-in",
        json={"email": MAIL, "password": PASS},
        headers={TRANSPORT_HEADER: "bearer"},
    )


# ── 1. Un endpoint, dos transportes ───────────────────────────────────────────
def test_el_cliente_cookie_no_recibe_tokens_en_el_cuerpo(client, contenedor):
    """
    Un cliente web no tiene dónde guardarlos que sea mejor que la cookie, así que devolverlos
    sólo agrega una copia que puede terminar en `localStorage` — lo que `HttpOnly` evita.
    """
    _alta(client)
    r = _sign_in_cookie(client)

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert "access_token" not in cuerpo
    assert "refresh_token" not in cuerpo
    assert cuerpo["session_id"]
    assert "set-cookie" in r.headers


def test_el_cliente_bearer_recibe_los_tokens_y_ninguna_cookie(client, contenedor):
    """Asimetría deliberada: un cliente nativo no tiene cookies y tiene que guardarlos él."""
    _alta(client)
    r = _sign_in_bearer(client)

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["access_token"]
    assert cuerpo["refresh_token"]
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_la_respuesta_declara_vary(client, contenedor):
    """
    Sin `Vary`, un cache compartido puede servirle a un usuario la respuesta de otro.
    """
    _alta(client)

    assert "Cookie" in _sign_in_cookie(client).headers.get("Vary", "")

    # Cliente nuevo: el de arriba ya tiene cookie de sesión, así que su próximo POST exige
    # CSRF — que es exactamente lo que el middleware tiene que hacer.
    limpio = TestClient(client.app, raise_server_exceptions=False)
    assert "Authorization" in _sign_in_bearer(limpio).headers.get("Vary", "")


def test_el_header_de_transporte_manda_sobre_la_cookie(client, contenedor):
    """
    Una webview nativa dentro de una sesión web manda las dos cosas; el header es la señal
    intencional y tiene que ganar.
    """
    _alta(client)
    _sign_in_cookie(client)  # el cliente ya tiene cookies

    # Con cookie de sesión presente, el POST necesita el double-submit: el middleware de CSRF
    # corre igual, y sólo después el router mira el transporte.
    r = client.post(
        "/auth/sign-in",
        json={"email": MAIL, "password": PASS},
        headers={TRANSPORT_HEADER: "bearer", **_csrf_headers(client)},
    )

    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


# ── 2. Los atributos de la cookie ─────────────────────────────────────────────
def test_los_atributos_de_la_cookie_son_los_seguros():
    """
    Aserteados literalmente sobre el header, no sobre la config: lo que protege es lo que el
    navegador recibe.

    `__Host-` es lo que impide que un subdominio comprometido **escriba** la cookie de sesión;
    `HttpOnly` es lo que impide que un XSS la **lea**. Son dos ataques distintos.
    """
    _configurar(secure_cookies=True, verified=True)
    try:
        app = create_app(
            features=AppFeatures(auth_context=True, health=False),
            routers=[build_identity_router()],
        )
        with TestClient(app, base_url="https://testserver") as client:
            _alta(client)
            r = _sign_in_cookie(client)

        crudas = r.headers.get_list("set-cookie")
        acceso = next(c for c in crudas if "__Host-session" in c)

        assert "__Host-session=" in acceso
        assert "HttpOnly" in acceso
        assert "Secure" in acceso
        assert "samesite=lax" in acceso.lower()
        assert "Path=/" in acceso
        assert "Domain=" not in acceso, (
            "con Domain el navegador rechaza el prefijo __Host- entero"
        )
    finally:
        reset_identity()
        asyncio.run(dispose_engine())


def test_la_cookie_de_csrf_es_legible_por_el_cliente(client, contenedor):
    """
    **No** es `HttpOnly`, y tiene que no serlo: el cliente la lee para devolverla en el
    header. Que sea legible es justamente lo que obliga a que su valor sea derivado del `sid`
    y no aleatorio.
    """
    _alta(client)
    r = _sign_in_cookie(client)

    csrf = next(c for c in r.headers.get_list("set-cookie") if c.startswith("csrf="))

    assert "HttpOnly" not in csrf


def test_el_valor_de_csrf_es_derivado_del_sid(client, contenedor):
    """
    Si fuera aleatorio, un subdominio comprometido podría escribir la cookie de CSRF y mandar
    el mismo valor en el header, pasando el double-submit con un valor que eligió él.
    """
    _alta(client)
    r = _sign_in_cookie(client)
    sid = r.json()["session_id"]

    assert client.cookies.get("csrf") == derive_csrf_token(sid, CLAVE)


# ── 3. El token va atado a su transporte ──────────────────────────────────────
def test_una_cookie_replayeada_como_bearer_se_rechaza(client, contenedor):
    """
    **El ataque de confusión de transporte.** Un token de cookie presentado como Bearer
    esquivaría `SameSite` y el chequeo anti-CSRF por completo. El `aud` lo ata.
    """
    _alta(client)
    _sign_in_cookie(client)
    de_cookie = client.cookies.get("session")
    assert de_cookie

    limpio = TestClient(client.app, raise_server_exceptions=False)
    r = limpio.get("/protegida", headers={"Authorization": f"Bearer {de_cookie}"})

    assert r.status_code == 401


def test_un_bearer_puesto_como_cookie_se_rechaza(client, contenedor):
    """El mismo ataque en el otro sentido."""
    _alta(client)
    token = _sign_in_bearer(client).json()["access_token"]

    limpio = TestClient(client.app, raise_server_exceptions=False)
    limpio.cookies.set("session", token)
    r = limpio.get("/protegida")

    assert r.status_code == 401


def test_cada_transporte_funciona_con_su_propio_token(client, contenedor):
    _alta(client)

    _sign_in_cookie(client)
    assert client.get("/protegida").json()["transport"] == "cookie"

    limpio = TestClient(client.app, raise_server_exceptions=False)
    token = _sign_in_bearer(limpio).json()["access_token"]
    otro = TestClient(client.app, raise_server_exceptions=False)
    r = otro.get("/protegida", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["transport"] == "bearer"


# ── 4. CSRF ───────────────────────────────────────────────────────────────────
def _csrf_headers(client: TestClient) -> dict[str, str]:
    return {"Origin": ORIGEN, CSRF_HEADER: client.cookies.get("csrf") or ""}


def test_un_post_cross_origin_con_cookie_valida_se_rechaza(client, contenedor):
    """El ataque que `SameSite` solo no cubre: un subdominio adyacente es same-site."""
    _alta(client)
    _sign_in_cookie(client)

    r = client.post(
        "/auth/sign-out",
        headers={"Origin": "https://evil.test", CSRF_HEADER: client.cookies.get("csrf") or ""},
    )

    assert r.status_code == 403


def test_un_post_del_origen_declarado_con_double_submit_pasa(client, contenedor):
    _alta(client)
    _sign_in_cookie(client)

    r = client.post("/auth/sign-out", headers=_csrf_headers(client))

    assert r.status_code == 200, r.text


def test_un_post_sin_el_header_de_csrf_se_rechaza(client, contenedor):
    _alta(client)
    _sign_in_cookie(client)

    r = client.post("/auth/sign-out", headers={"Origin": ORIGEN})

    assert r.status_code == 403


def test_un_valor_de_csrf_forjado_se_rechaza(client, contenedor):
    """
    Lo que un subdominio comprometido podría escribir: un valor que él eligió. No verifica,
    porque el esperado es el HMAC del `sid`.
    """
    _alta(client)
    _sign_in_cookie(client)

    r = client.post(
        "/auth/sign-out",
        headers={"Origin": ORIGEN, CSRF_HEADER: "valor-inventado-por-el-atacante"},
    )

    assert r.status_code == 403


def test_un_get_esta_exento_de_csrf(client, contenedor):
    """`GET` no cambia estado. Exigirle el header rompería toda navegación normal."""
    _alta(client)
    _sign_in_cookie(client)

    assert client.get("/protegida", headers={"Origin": "https://evil.test"}).status_code == 200


def test_el_csrf_no_aplica_al_transporte_bearer(client, contenedor):
    """
    Un cliente Bearer adjunta el token a propósito en cada petición, así que un origen ajeno
    no puede provocar una petición autenticada. Exigirle CSRF sería un chequeo que nunca se
    dispara y que rompe a los clientes nativos.
    """
    _alta(client)
    token = _sign_in_bearer(client).json()["access_token"]

    limpio = TestClient(client.app, raise_server_exceptions=False)
    r = limpio.post("/auth/sign-out", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text


def test_un_post_anonimo_no_pide_csrf(client, contenedor):
    """
    Sin cookie de sesión no hay nada que un origen ajeno pueda aprovechar. Exigirlo rompería
    el propio sign-in, que es un POST anónimo.
    """
    r = client.post(
        "/auth/sign-in",
        json={"email": "nadie@test.com", "password": "x"},
        headers={"Origin": "https://cualquiera.test"},
    )

    assert r.status_code == 401  # rechazado por credenciales, no por CSRF


# ── 5. 401 con WWW-Authenticate ───────────────────────────────────────────────
def test_un_401_lleva_www_authenticate(client, contenedor):
    """RFC 6750 §3. Sólo es posible por el `headers_for` que la Fase 0 agregó."""
    r = client.get("/protegida")

    assert r.status_code == 401
    assert "Bearer" in r.headers.get("WWW-Authenticate", "")


def test_un_403_no_lleva_www_authenticate(client, contenedor):
    """
    El header describe **cómo autenticarse**, así que en un 403 —ya sabemos quién sos— no
    tiene sentido y confundiría al cliente para que reintente el login.
    """
    _alta(client)
    _sign_in_cookie(client)

    r = client.get("/con-scope")

    assert r.status_code == 403
    assert "WWW-Authenticate" not in r.headers


def test_el_cuerpo_del_error_conserva_la_forma_del_framework(client, contenedor):
    r = client.get("/protegida")

    cuerpo = r.json()
    assert cuerpo["error"] == "UnauthenticatedError"
    assert "detail" in cuerpo


def test_el_mapa_de_darwin_se_mergea_solo_con_auth_context(contenedor):
    """
    `IDENTITY_EXCEPTION_STATUS_MAP` **no** está en `DEFAULT_EXCEPTION_STATUS_MAP`: importar
    las excepciones de Darwin en tiempo de import de la capa `api` la acoplaría al módulo y
    rompería el contrato de dependencias opcionales.
    """
    from hexcore.darwin import UnauthenticatedError

    sin_auth = create_app(features=AppFeatures(auth_context=False, health=False))

    @sin_auth.get("/x")
    async def x() -> None:
        raise UnauthenticatedError("sin mapear")

    r = TestClient(sin_auth, raise_server_exceptions=False).get("/x")

    # Sin el feature, la excepción no está mapeada: sale como 500, no como 401.
    assert r.status_code == 500


# ── 6. Fijación de sesión ─────────────────────────────────────────────────────
def test_el_token_cambia_en_cada_sign_in(client, contenedor):
    _alta(client)
    primero = _sign_in_bearer(client).json()["access_token"]
    segundo = _sign_in_bearer(client).json()["access_token"]

    assert primero != segundo


def test_el_refresh_rota_el_token(client, contenedor):
    _alta(client)
    par = _sign_in_bearer(client).json()

    r = client.post(
        "/auth/refresh",
        headers={
            TRANSPORT_HEADER: "bearer",
            "X-Refresh-Token": par["refresh_token"],
        },
    )

    assert r.status_code == 200, r.text
    assert r.json()["access_token"] != par["access_token"]


def test_el_sign_out_borra_las_cookies_y_revoca(client, contenedor):
    """
    Las dos cosas: borrar la cookie sin revocar deja el token vivo para quien lo copió, y
    revocar sin borrar la cookie le deja al navegador una credencial muerta.
    """
    _alta(client)
    _sign_in_cookie(client)
    assert client.get("/protegida").status_code == 200

    r = client.post("/auth/sign-out", headers=_csrf_headers(client))

    assert r.status_code == 200
    assert not client.cookies.get("session")
    assert client.get("/protegida").status_code == 401


def test_sign_out_everywhere_revoca_todas(client, contenedor):
    _alta(client)
    bearer = _sign_in_bearer(client).json()
    _sign_in_cookie(client)

    r = client.post("/auth/sign-out-everywhere", headers=_csrf_headers(client))
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] >= 1

    otro = TestClient(client.app, raise_server_exceptions=False)
    caido = otro.get(
        "/protegida", headers={"Authorization": f"Bearer {bearer['access_token']}"}
    )
    assert caido.status_code == 401


# ── 7. El ContextVar ──────────────────────────────────────────────────────────
def test_una_ruta_publica_ve_el_contexto_si_hay_sesion(client, contenedor):
    """
    La doble publicación: el endpoint puede leer `current_auth()` sin declarar dependencias.
    """
    _alta(client)
    assert client.get("/publica").json()["hay_contexto"] is False

    _sign_in_cookie(client)
    assert client.get("/publica").json()["hay_contexto"] is True


def test_el_contexto_se_limpia_aunque_el_endpoint_lance(client, contenedor):
    """
    El `reset` va en un `finally`: sin eso, un endpoint que falla deja el contexto colgado
    para la corutina siguiente que reuse el mismo task — filtrado de identidad entre requests.
    """
    _alta(client)
    _sign_in_cookie(client)

    assert client.get("/explota").status_code == 500
    assert current_auth() is None


def test_una_credencial_invalida_deja_el_request_anonimo(client, contenedor):
    """
    El middleware no rechaza: publica `None` y deja pasar. Rechazar ahí le daría 401 a las
    rutas públicas de un cliente cuyo token acaba de vencer — incluida la de refresh.
    """
    limpio = TestClient(client.app, raise_server_exceptions=False)
    limpio.cookies.set("session", "esto-no-es-un-token")

    assert limpio.get("/publica").status_code == 200
    assert limpio.get("/publica").json()["hay_contexto"] is False
    assert limpio.get("/protegida").status_code == 401


# ── El router ─────────────────────────────────────────────────────────────────
def test_me_devuelve_actor_y_subject(client, contenedor):
    """
    Los dos ids incluso acá: un operador que impersona tiene que ver en su propia UI que está
    dentro de la cuenta de otro.
    """
    _alta(client)
    _sign_in_cookie(client)

    cuerpo = client.get("/auth/me").json()

    assert cuerpo["actor_id"] == cuerpo["subject_id"]
    assert cuerpo["impersonating"] is False
    assert cuerpo["email"] == MAIL


def test_sessions_no_expone_el_hash_del_token(client, contenedor):
    """
    Publicar el hash de una credencial no aporta nada y sí le da a un atacante con acceso de
    lectura el índice por el que buscar.
    """
    _alta(client)
    _sign_in_cookie(client)

    sesiones = client.get("/auth/sessions").json()

    assert len(sesiones) == 1
    assert "token_hash" not in sesiones[0]
    assert sesiones[0]["current"] is True


def test_el_refresh_sin_token_da_401(client, contenedor):
    r = client.post("/auth/refresh", headers={TRANSPORT_HEADER: "bearer"})

    assert r.status_code == 401
    assert "X-Refresh-Token" in r.json()["detail"]


def test_el_sign_in_esta_limitado_por_ip(client, contenedor):
    """
    Con el `rate_limit` corregido en la Fase 0: `client_ip_key` ya no confía en XFF y el
    conteo es atómico. Sin las dos cosas el límite era un no-op.
    """
    for _ in range(5):
        client.post("/auth/sign-in", json={"email": MAIL, "password": "mal"})

    r = client.post("/auth/sign-in", json={"email": MAIL, "password": "mal"})

    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_el_router_se_puede_montar_sin_sign_up(contenedor):
    """Para una app donde las cuentas las crea un administrador."""
    app = create_app(
        features=AppFeatures(auth_context=True, health=False),
        routers=[build_identity_router(include_sign_up=False)],
    )
    rutas = {r.path for r in app.routes}  # type: ignore[attr-defined]

    assert "/auth/sign-up" not in rutas
    assert "/auth/sign-in" in rutas


# ── Los features están apagados por defecto ───────────────────────────────────
def test_auth_context_y_csrf_estan_apagados_por_defecto():
    """
    Prenderlos en silencio cambiaría el comportamiento de toda app existente: `create_app()`
    empezaría a resolver credenciales y a exigir que Darwin esté configurado.
    """
    features = AppFeatures()

    assert features.auth_context is False
    assert features.csrf is False


def test_una_app_sin_el_feature_no_resuelve_credenciales(contenedor):
    app = create_app(features=AppFeatures(health=False))

    @app.get("/x")
    async def x() -> dict[str, bool]:
        return {"hay_contexto": current_auth() is not None}

    r = TestClient(app).get("/x")

    assert r.json()["hay_contexto"] is False
