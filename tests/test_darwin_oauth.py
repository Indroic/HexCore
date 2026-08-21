"""
Darwin Fase 9: `oauth`, contra SQLite y un proveedor falso.

El proveedor es un doble del puerto `AbstractOAuthHttpClient` y no un servidor HTTP: el flujo
completo se ejercita sin red, y el test puede hacer que el proveedor mienta —devolver un mail
que no verificó, un `account_id` distinto, un 400— que es justamente lo que hay que probar.

Lo adversarial que se fija:

- **PKCE es `S256` y el verificador nunca aparece en la URL.** Si apareciera, PKCE no protegería
  nada: cualquiera que vea la URL en el historial o en un log podría canjear el código.
- **El `state` es de un solo uso**, está atado al proveedor y al `redirect_uri`, y vence.
- **No se vincula por coincidencia de mail** con la política por default. Es la toma de cuentas
  más común de OAuth, y el test la ejecuta: un atacante con el mail de la víctima en otro
  proveedor **no** entra a su cuenta.
- Con `VERIFIED_EMAIL` hacen falta **las dos** verificaciones, no una.
- **A quién se vincula sale del `state`, no del callback.**
- **Los tokens del proveedor se guardan cifrados.**
- **Desvincular no puede dejar la cuenta sin acceso.**
- Una identidad ya vinculada a otra cuenta **no se mueve**.
"""
from __future__ import annotations

import asyncio
import typing as t
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    FixedClock,
    IdentityConfig,
    PluginRegistry,
    StaticKeyStore,
    TokenConfig,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    reset_identity,
)
from hexcore.darwin.plugins.oauth import (  # noqa: E402
    AbstractOAuthHttpClient,
    LinkPolicy,
    OAuthAccountAlreadyLinkedError,
    OAuthAccountNotLinkedError,
    OAuthEmailNotVerifiedError,
    OAuthError,
    OAuthExchangeError,
    OAuthPlugin,
    OAuthProviderNotConfiguredError,
    OAuthStateError,
    OAuthTokens,
    get_oauth_service,
)
from hexcore.darwin.plugins.oauth.domain import (  # noqa: E402
    CODE_CHALLENGE_METHOD,
    generate_pkce_verifier,
    pkce_challenge,
)
from hexcore.darwin.plugins.oauth.models import create_oauth_tables  # noqa: E402
from hexcore.darwin.plugins.oauth.providers import (  # noqa: E402
    OAuthProfile,
    OAuthProvider,
    discord,
    github,
    google,
    microsoft,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48
PASS = "una frase larga y buena"
CALLBACK = "https://mi-app.test/auth/callback"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── PKCE ──────────────────────────────────────────────────────────────────────
class TestPkce:
    def test_el_verificador_esta_en_el_rango_de_la_rfc(self):
        """RFC 7636 §4.1: de 43 a 128 caracteres."""
        for _ in range(20):
            verificador = generate_pkce_verifier()
            assert 43 <= len(verificador) <= 128
            assert "=" not in verificador

    def test_dos_verificadores_no_se_repiten(self):
        assert generate_pkce_verifier() != generate_pkce_verifier()

    def test_el_desafio_es_sha256_base64url_sin_relleno(self):
        import base64
        import hashlib

        verificador = generate_pkce_verifier()
        esperado = (
            base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest())
            .decode()
            .rstrip("=")
        )

        assert pkce_challenge(verificador) == esperado
        assert "=" not in pkce_challenge(verificador)

    def test_el_metodo_es_s256_y_no_plain(self):
        """
        `plain` manda el verificador en la URL de autorización, o sea que cualquiera que la vea
        —historial, log de proxy, `Referer`— puede canjear el código. La RFC lo permite sólo
        para clientes que no pueden hacer SHA-256, que en Python no existen.
        """
        assert CODE_CHALLENGE_METHOD == "S256"

    def test_el_desafio_no_revela_el_verificador(self):
        verificador = generate_pkce_verifier()

        assert verificador not in pkce_challenge(verificador)


# ── Los proveedores ───────────────────────────────────────────────────────────
class TestProveedores:
    def test_los_preconfigurados_se_arman(self):
        for fabrica in (google, github, microsoft, discord):
            proveedor = fabrica(client_id="id", client_secret="secreto")
            assert proveedor.id
            assert proveedor.authorize_url.startswith("https://")
            assert proveedor.client_secret.get_secret_value() == "secreto"

    def test_el_secreto_no_aparece_en_el_repr(self):
        """`SecretStr`: un traceback de pytest imprime los locals."""
        proveedor = google(client_id="id", client_secret="el-secreto-real")

        assert "el-secreto-real" not in repr(proveedor)

    def test_google_pide_access_type_offline(self):
        """
        Sin eso Google **no devuelve refresh token**, y el access token guardado deja de servir
        en una hora sin forma de renovarlo. Es el detalle que hace que la integración parezca
        funcionar en el test y falle al día siguiente.
        """
        assert google(client_id="a", client_secret="b").extra_authorize_params == {
            "access_type": "offline"
        }

    def test_una_url_http_se_rechaza(self):
        """Por HTTP viajarían en claro el `code` y el `client_secret`."""
        with pytest.raises(ValueError, match="HTTPS"):
            OAuthProvider(
                id="malo",
                client_id="a",
                client_secret="b",  # type: ignore[arg-type]
                authorize_url="http://ejemplo.com/auth",
                token_url="https://ejemplo.com/token",
                userinfo_url="https://ejemplo.com/me",
                parse_profile=lambda d: OAuthProfile(account_id="x"),
            )

    def test_localhost_por_http_se_permite(self):
        """Para poder correr un proveedor falso en desarrollo."""
        proveedor = OAuthProvider(
            id="local",
            client_id="a",
            client_secret="b",  # type: ignore[arg-type]
            authorize_url="http://localhost:9000/auth",
            token_url="http://localhost:9000/token",
            userinfo_url="http://localhost:9000/me",
            parse_profile=lambda d: OAuthProfile(account_id="x"),
        )

        assert proveedor.id == "local"

    def test_el_perfil_no_verificado_es_el_default(self):
        """
        Un proveedor que no informa `email_verified` se trata como no verificado. Al revés
        —asumir verificado cuando falta— es el agujero de toma de cuentas.
        """
        assert OAuthProfile(account_id="1").email_verified is False

    def test_el_parser_oidc_normaliza_el_string_true(self):
        """Los proveedores no se ponen de acuerdo entre booleano y string."""
        proveedor = google(client_id="a", client_secret="b")

        assert proveedor.parse_profile({"sub": "1", "email_verified": "true"}).email_verified
        assert proveedor.parse_profile({"sub": "1", "email_verified": True}).email_verified
        assert not proveedor.parse_profile({"sub": "1", "email_verified": "no"}).email_verified

    def test_github_nunca_marca_el_mail_verificado(self):
        """
        `/user` no dice si el mail está verificado. Asumirlo sería tomar la palabra de un campo
        que el proveedor no informa.
        """
        perfil = github(client_id="a", client_secret="b").parse_profile(
            {"id": 42, "email": "ana@ejemplo.com", "login": "ana"}
        )

        assert perfil.account_id == "42"
        assert perfil.email_verified is False

    def test_dos_proveedores_con_el_mismo_id_se_rechazan(self):
        """
        Uno ganaría en silencio según el orden de la lista, y `provider_id` es parte de la clave
        única de `account`: el que pierde desvincularía a sus usuarios.
        """
        with pytest.raises(ValueError, match="mismo `id`"):
            OAuthPlugin(
                providers=[
                    google(client_id="a", client_secret="b"),
                    google(client_id="c", client_secret="d"),
                ]
            )


# ── El proveedor falso ────────────────────────────────────────────────────────
class ProveedorFalso(AbstractOAuthHttpClient):
    """
    Un doble del puerto HTTP.

    Registra con qué lo llamaron —para poder aseverar que el `code_verifier` viajó y que el
    `redirect_uri` fue el correcto— y deja que el test le haga devolver cualquier cosa, incluido
    un error.
    """

    def __init__(
        self,
        *,
        perfil: dict[str, t.Any] | None = None,
        tokens: OAuthTokens | None = None,
        falla_canje: bool = False,
        falla_perfil: bool = False,
    ) -> None:
        self.perfil = perfil or {
            "sub": "prov-123",
            "email": "ana@ejemplo.com",
            "email_verified": True,
            "name": "Ana",
        }
        self.tokens = tokens or OAuthTokens(
            access_token="at-del-proveedor",
            refresh_token="rt-del-proveedor",
            expires_in=3600,
            scope="openid email",
        )
        self.falla_canje = falla_canje
        self.falla_perfil = falla_perfil
        self.canjes: list[dict[str, t.Any]] = []
        self.perfiles: list[str] = []

    async def exchange_code(
        self,
        token_url: str,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: str,
    ) -> OAuthTokens:
        self.canjes.append(
            {
                "token_url": token_url,
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            }
        )
        if self.falla_canje:
            raise OAuthExchangeError("el proveedor dijo que no")
        return self.tokens

    async def fetch_profile(
        self, userinfo_url: str, *, access_token: str
    ) -> dict[str, t.Any]:
        self.perfiles.append(access_token)
        if self.falla_perfil:
            raise OAuthExchangeError("el proveedor no dio el perfil")
        return self.perfil


# ── El cableado ───────────────────────────────────────────────────────────────
@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def falso() -> ProveedorFalso:
    return ProveedorFalso()


def _armar(reloj, falso, *, policy=LinkPolicy.NEVER, redirects=(CALLBACK,)):
    plugin = OAuthPlugin(
        providers=[google(client_id="cliente", client_secret="secreto")],
        allowed_redirect_uris=redirects,
        link_policy=policy,
        http=falso,
    )
    reset_identity()
    plugin.reset()
    contenedor = configure_identity(
        IdentityConfig(
            secret_key=CLAVE,
            tokens=TokenConfig(issuer="https://api.test"),
            require_verified_email=False,
        ),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        plugins=PluginRegistry([plugin]),
    )
    return contenedor, plugin


@pytest.fixture
def base():
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))
    asyncio.run(create_oauth_tables(motor))

    from hexcore.config import LazyConfig
    from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

    LazyConfig.get_config().cache_backend = MemoryCache()
    yield motor

    reset_identity()
    asyncio.run(dispose_engine())


@pytest.fixture
def contenedor(base, reloj, falso):
    contenedor, _ = _armar(reloj, falso)
    yield contenedor
    reset_identity()


@pytest.fixture
def servicio(contenedor):
    return get_oauth_service()


async def _flujo(servicio, falso, *, link_user_id=None):
    """Inicia el flujo y devuelve `(state, autorizacion)`."""
    autorizacion = await servicio.start(
        "google", redirect_uri=CALLBACK, link_user_id=link_user_id
    )
    return autorizacion.state, autorizacion


# ── Iniciar ───────────────────────────────────────────────────────────────────
class TestStart:
    @pytest.mark.anyio
    async def test_la_url_lleva_todo_lo_que_pide_la_spec(self, servicio):
        autorizacion = await servicio.start("google", redirect_uri=CALLBACK)

        partes = urlparse(autorizacion.url)
        query = parse_qs(partes.query)

        assert f"{partes.scheme}://{partes.netloc}{partes.path}" == (
            "https://accounts.google.com/o/oauth2/v2/auth"
        )
        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["cliente"]
        assert query["redirect_uri"] == [CALLBACK]
        assert query["state"] == [autorizacion.state]
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"], "el desafío de PKCE va en la URL"
        assert query["scope"] == ["openid email profile"]
        assert query["access_type"] == ["offline"]

    @pytest.mark.anyio
    async def test_el_verificador_de_pkce_no_va_en_la_url(self, servicio, base):
        """
        **Toda la protección de PKCE.** Si el verificador viajara en la URL, cualquiera que la
        vea en el historial, en un log de proxy o en un `Referer` podría canjear el código.
        """
        autorizacion = await servicio.start("google", redirect_uri=CALLBACK)

        from sqlalchemy import select

        from hexcore.darwin.plugins.oauth.models import OAuthStateModel
        from hexcore.infrastructure.uow.scopes import session_scope

        async with session_scope() as sesion:
            fila = (await sesion.execute(select(OAuthStateModel))).scalar_one()

        from hexcore.darwin.infrastructure.secretbox import SecretBox
        from hexcore.darwin.plugins.oauth import _ETIQUETA_TOKENS

        verificador = SecretBox(CLAVE, label=_ETIQUETA_TOKENS).decrypt(
            fila.code_verifier_encrypted
        )

        assert verificador not in autorizacion.url
        # Y el desafío de la URL corresponde a ese verificador.
        assert pkce_challenge(verificador) in autorizacion.url

    @pytest.mark.anyio
    async def test_el_state_se_guarda_hasheado(self, servicio, base):
        """
        El `state` viaja por la URL y queda en el historial y en los logs del proveedor: un dump
        de la tabla no debería sumar la capacidad de completar un flujo ajeno.
        """
        from hexcore.darwin.infrastructure.hashing import hash_token

        autorizacion = await servicio.start("google", redirect_uri=CALLBACK)

        from sqlalchemy import select

        from hexcore.darwin.plugins.oauth.models import OAuthStateModel
        from hexcore.infrastructure.uow.scopes import session_scope

        async with session_scope() as sesion:
            fila = (await sesion.execute(select(OAuthStateModel))).scalar_one()

        assert fila.state_hash == hash_token(autorizacion.state)
        assert fila.state_hash != autorizacion.state

    @pytest.mark.anyio
    async def test_un_proveedor_no_configurado_falla(self, servicio):
        with pytest.raises(OAuthProviderNotConfiguredError, match="github"):
            await servicio.start("github", redirect_uri=CALLBACK)

    @pytest.mark.anyio
    async def test_un_redirect_uri_fuera_de_la_allowlist_se_rechaza(self, servicio):
        """
        Sin esto, un atacante inicia el flujo apuntando a su propio sitio y se lleva el código
        de la víctima — el proveedor redirige a donde le digan si la URI está registrada con un
        comodín, y muchas lo están.
        """
        with pytest.raises(ValueError, match="no está en la lista"):
            await servicio.start("google", redirect_uri="https://evil.test/cb")

    @pytest.mark.anyio
    async def test_sin_allowlist_no_se_valida(self, base, reloj, falso):
        """Deliberado, para que un test o un desarrollo local no tengan que declararla."""
        _, plugin = _armar(reloj, falso, redirects=())
        try:
            autorizacion = await plugin.service().start(
                "google", redirect_uri="http://localhost:3000/cb"
            )
            assert autorizacion.url
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_dos_flujos_dan_states_distintos(self, servicio):
        uno = await servicio.start("google", redirect_uri=CALLBACK)
        otro = await servicio.start("google", redirect_uri=CALLBACK)

        assert uno.state != otro.state


# ── El callback ───────────────────────────────────────────────────────────────
class TestCallback:
    @pytest.mark.anyio
    async def test_crea_el_usuario_la_primera_vez(self, servicio, falso, contenedor):
        estado, _ = await _flujo(servicio, falso)

        entrada = await servicio.callback(
            "google", code="el-code", state=estado, redirect_uri=CALLBACK
        )

        assert entrada.created is True
        assert entrada.user.email == "ana@ejemplo.com"
        assert entrada.user.email_verified is True
        assert entrada.tokens.access_token
        assert entrada.session.actor_user_id == entrada.user.id

    @pytest.mark.anyio
    async def test_la_segunda_vez_entra_a_la_misma_cuenta(self, servicio, falso):
        estado, _ = await _flujo(servicio, falso)
        primera = await servicio.callback(
            "google", code="c1", state=estado, redirect_uri=CALLBACK
        )

        estado, _ = await _flujo(servicio, falso)
        segunda = await servicio.callback(
            "google", code="c2", state=estado, redirect_uri=CALLBACK
        )

        assert segunda.created is False
        assert segunda.user.id == primera.user.id

    @pytest.mark.anyio
    async def test_el_canje_recibe_el_verificador_y_el_secreto(self, servicio, falso):
        estado, _ = await _flujo(servicio, falso)
        await servicio.callback(
            "google", code="el-code", state=estado, redirect_uri=CALLBACK
        )

        canje = falso.canjes[0]

        assert canje["code"] == "el-code"
        assert canje["redirect_uri"] == CALLBACK
        assert canje["client_secret"] == "secreto"
        assert 43 <= len(canje["code_verifier"]) <= 128

    @pytest.mark.anyio
    async def test_el_perfil_sale_del_userinfo_con_el_access_token(self, servicio, falso):
        """
        Y no del `id_token`: verificarlo bien exige traer y cachear el JWKS de cada proveedor, y
        usarlo sin verificar es peor que no usarlo.
        """
        estado, _ = await _flujo(servicio, falso)
        await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        assert falso.perfiles == ["at-del-proveedor"]

    @pytest.mark.anyio
    async def test_el_email_verified_del_proveedor_se_respeta(self, base, reloj):
        """
        Un usuario creado por OAuth con el mail marcado verificado sin que el proveedor lo diga
        es una afirmación que nadie comprobó — y de la que después dependen otros flujos.
        """
        falso = ProveedorFalso(
            perfil={"sub": "p1", "email": "sin@verificar.test", "email_verified": False}
        )
        _, plugin = _armar(reloj, falso)
        try:
            servicio = plugin.service()
            estado, _ = await _flujo(servicio, falso)
            entrada = await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )

            assert entrada.user.email_verified is False
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_un_perfil_sin_account_id_se_rechaza(self, base, reloj):
        falso = ProveedorFalso(perfil={"email": "sin@id.test"})
        _, plugin = _armar(reloj, falso)
        try:
            servicio = plugin.service()
            estado, _ = await _flujo(servicio, falso)

            with pytest.raises(OAuthStateError, match="identificador"):
                await servicio.callback(
                    "google", code="c", state=estado, redirect_uri=CALLBACK
                )
        finally:
            reset_identity()


# ── El `state` ────────────────────────────────────────────────────────────────
class TestState:
    @pytest.mark.anyio
    async def test_es_de_un_solo_uso(self, servicio, falso):
        estado, _ = await _flujo(servicio, falso)
        await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        with pytest.raises(OAuthStateError):
            await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )

    @pytest.mark.anyio
    async def test_un_state_inventado_se_rechaza(self, servicio):
        with pytest.raises(OAuthStateError):
            await servicio.callback(
                "google", code="c", state="inventado", redirect_uri=CALLBACK
            )

    @pytest.mark.anyio
    async def test_no_se_llama_al_proveedor_con_un_state_malo(self, servicio, falso):
        """
        El `state` se consume **antes** de hablar con el proveedor: así un `state` inválido no
        gasta una llamada de red contra un tercero.
        """
        with pytest.raises(OAuthStateError):
            await servicio.callback(
                "google", code="c", state="inventado", redirect_uri=CALLBACK
            )

        assert falso.canjes == []

    @pytest.mark.anyio
    async def test_un_state_vencido_se_rechaza(self, servicio, falso, reloj):
        estado, _ = await _flujo(servicio, falso)

        reloj.advance(minutes=11)  # el TTL por default son 10

        with pytest.raises(OAuthStateError):
            await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )

    @pytest.mark.anyio
    async def test_el_redirect_uri_del_callback_tiene_que_coincidir(
        self, base, reloj, falso
    ):
        """
        El proveedor ya lo valida contra el suyo, pero eso no cubre dos URIs ambas registradas:
        sin este chequeo, un flujo iniciado para una se puede completar en la otra.
        """
        otra = "https://mi-app.test/otro-callback"
        _, plugin = _armar(reloj, falso, redirects=(CALLBACK, otra))
        try:
            servicio = plugin.service()
            estado, _ = await _flujo(servicio, falso)

            with pytest.raises(OAuthStateError, match="redirect_uri"):
                await servicio.callback(
                    "google", code="c", state=estado, redirect_uri=otra
                )
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_ocho_callbacks_concurrentes_dejan_pasar_uno(self, servicio, falso):
        """
        Es la razón por la que `consume` es un `UPDATE ... WHERE consumed_at IS NULL RETURNING`.
        Con leer-y-después-escribir, el `state` deja de ser de un solo uso — y eso es la mitad
        de su valor como defensa anti-CSRF.
        """
        estado, _ = await _flujo(servicio, falso)

        resultados = await asyncio.gather(
            *(
                servicio.callback(
                    "google", code="c", state=estado, redirect_uri=CALLBACK
                )
                for _ in range(8)
            ),
            return_exceptions=True,
        )

        ganaron = [r for r in resultados if not isinstance(r, BaseException)]
        perdieron = [r for r in resultados if isinstance(r, OAuthStateError)]

        assert len(ganaron) == 1, f"ganaron {len(ganaron)}"
        assert len(perdieron) == 7


# ── La vinculación por mail: la toma de cuentas ───────────────────────────────
class TestVinculacionPorMail:
    @pytest.mark.anyio
    async def test_el_default_no_vincula_por_mail(self, contenedor, servicio, falso):
        """
        ⚠️ **El test de la toma de cuentas más común de OAuth.**

        Ana tiene cuenta local con `ana@ejemplo.com`. Un atacante consigue registrar ese mail en
        un proveedor —hay IdPs que no verifican, u otros donde el mail se puede cambiar sin
        re-verificar— e inicia el flujo. Con vinculación automática, entra a la cuenta de Ana.
        Con el default, recibe un 409 que le explica que la vinculación tiene que ser explícita.
        """
        await contenedor.identity_service().sign_up(
            email="ana@ejemplo.com", password=PASS
        )
        estado, _ = await _flujo(servicio, falso)

        with pytest.raises(OAuthAccountNotLinkedError) as excinfo:
            await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )

        mensaje = str(excinfo.value)
        assert "vinculá" in mensaje.lower()
        assert "google" in mensaje

    @pytest.mark.anyio
    async def test_y_no_deja_ninguna_cuenta_vinculada(self, contenedor, servicio, falso):
        """El rechazo no puede dejar la vinculación hecha a medias."""
        usuario, _ = await contenedor.identity_service().sign_up(
            email="ana@ejemplo.com", password=PASS
        )
        estado, _ = await _flujo(servicio, falso)

        with pytest.raises(OAuthAccountNotLinkedError):
            await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )

        assert await servicio.list_linked(usuario.id) == []

    @pytest.mark.anyio
    async def test_verified_email_exige_las_dos_verificaciones(self, base, reloj):
        """
        La del proveedor **y** la local. Cada una sola deja una mitad del agujero abierta: sin
        la del proveedor, cualquiera registra el mail ajeno; sin la local, la cuenta local pudo
        haberse creado con un mail que su dueño nunca confirmó.
        """
        falso = ProveedorFalso(
            perfil={"sub": "p1", "email": "ana@ejemplo.com", "email_verified": False}
        )
        contenedor, plugin = _armar(reloj, falso, policy=LinkPolicy.VERIFIED_EMAIL)
        try:
            servicio = plugin.service()
            usuario, _ = await contenedor.identity_service().sign_up(
                email="ana@ejemplo.com", password=PASS
            )
            await contenedor.users().update(
                usuario.model_copy(update={"email_verified": True})
            )

            estado, _ = await _flujo(servicio, falso)
            with pytest.raises(OAuthEmailNotVerifiedError):
                await servicio.callback(
                    "google", code="c", state=estado, redirect_uri=CALLBACK
                )
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_verified_email_rechaza_si_la_local_no_verifico(self, base, reloj):
        falso = ProveedorFalso(
            perfil={"sub": "p1", "email": "ana@ejemplo.com", "email_verified": True}
        )
        contenedor, plugin = _armar(reloj, falso, policy=LinkPolicy.VERIFIED_EMAIL)
        try:
            servicio = plugin.service()
            await contenedor.identity_service().sign_up(
                email="ana@ejemplo.com", password=PASS
            )  # queda sin verificar

            estado, _ = await _flujo(servicio, falso)
            with pytest.raises(OAuthAccountNotLinkedError, match="verificó"):
                await servicio.callback(
                    "google", code="c", state=estado, redirect_uri=CALLBACK
                )
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_verified_email_vincula_con_las_dos(self, base, reloj):
        falso = ProveedorFalso(
            perfil={"sub": "p1", "email": "ana@ejemplo.com", "email_verified": True}
        )
        contenedor, plugin = _armar(reloj, falso, policy=LinkPolicy.VERIFIED_EMAIL)
        try:
            servicio = plugin.service()
            usuario, _ = await contenedor.identity_service().sign_up(
                email="ana@ejemplo.com", password=PASS
            )
            await contenedor.users().update(
                usuario.model_copy(update={"email_verified": True})
            )

            estado, _ = await _flujo(servicio, falso)
            entrada = await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )

            assert entrada.created is False
            assert entrada.user.id == usuario.id
            assert await servicio.list_linked(usuario.id) == ["google"]
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_any_email_vincula_sin_preguntar(self, base, reloj):
        """
        Existe sólo para migraciones desde sistemas que ya lo hacían. El test lo documenta como
        lo que es: la vinculación insegura, disponible a propósito y con la advertencia puesta.
        """
        falso = ProveedorFalso(
            perfil={"sub": "p1", "email": "ana@ejemplo.com", "email_verified": False}
        )
        contenedor, plugin = _armar(reloj, falso, policy=LinkPolicy.ANY_EMAIL)
        try:
            servicio = plugin.service()
            usuario, _ = await contenedor.identity_service().sign_up(
                email="ana@ejemplo.com", password=PASS
            )

            estado, _ = await _flujo(servicio, falso)
            entrada = await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )

            assert entrada.user.id == usuario.id
        finally:
            reset_identity()


# ── La vinculación explícita ──────────────────────────────────────────────────
class TestVincular:
    @pytest.mark.anyio
    async def test_vincular_desde_una_sesion_existente(
        self, contenedor, servicio, falso
    ):
        """El camino seguro: el usuario ya autenticado suma un proveedor."""
        usuario, _ = await contenedor.identity_service().sign_up(
            email="ana@ejemplo.com", password=PASS
        )

        estado, _ = await _flujo(servicio, falso, link_user_id=usuario.id)
        entrada = await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        assert entrada.user.id == usuario.id
        assert entrada.created is False
        assert await servicio.list_linked(usuario.id) == ["google"]

    @pytest.mark.anyio
    async def test_a_quien_se_vincula_sale_del_state(self, contenedor, servicio, falso):
        """
        Y no del callback, que lo controla en parte quien maneja el navegador. Se prueba con dos
        usuarios: el flujo iniciado por Beto vincula a Beto, aunque el perfil del proveedor
        traiga el mail de Ana.
        """
        await contenedor.identity_service().sign_up(
            email="ana@ejemplo.com", password=PASS
        )
        beto, _ = await contenedor.identity_service().sign_up(
            email="beto@ejemplo.com", password=PASS
        )

        estado, _ = await _flujo(servicio, falso, link_user_id=beto.id)
        entrada = await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        assert entrada.user.id == beto.id
        assert await servicio.list_linked(beto.id) == ["google"]

    @pytest.mark.anyio
    async def test_una_identidad_ya_vinculada_a_otro_no_se_mueve(
        self, contenedor, servicio, falso
    ):
        """
        Moverla dejaría a la primera cuenta sin su método de acceso, y si era el único, sin
        acceso.
        """
        estado, _ = await _flujo(servicio, falso)
        primero = await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        otro, _ = await contenedor.identity_service().sign_up(
            email="otro@ejemplo.com", password=PASS
        )
        estado, _ = await _flujo(servicio, falso, link_user_id=otro.id)

        with pytest.raises(OAuthAccountAlreadyLinkedError):
            await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )

        assert await servicio.list_linked(primero.user.id) == ["google"]
        assert await servicio.list_linked(otro.id) == []

    @pytest.mark.anyio
    async def test_revincular_la_misma_identidad_refresca_los_tokens(
        self, contenedor, servicio, falso
    ):
        usuario, _ = await contenedor.identity_service().sign_up(
            email="ana@ejemplo.com", password=PASS
        )
        estado, _ = await _flujo(servicio, falso, link_user_id=usuario.id)
        await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        falso.tokens = OAuthTokens(access_token="at-nuevo", expires_in=3600)
        estado, _ = await _flujo(servicio, falso, link_user_id=usuario.id)
        await servicio.callback(
            "google", code="c2", state=estado, redirect_uri=CALLBACK
        )

        cuentas = await contenedor.accounts().list_for_user(usuario.id)
        google_cuenta = next(c for c in cuentas if c.provider_id == "google")
        assert servicio.decrypt_access_token(google_cuenta) == "at-nuevo"


# ── Los tokens del proveedor ──────────────────────────────────────────────────
class TestTokensDelProveedor:
    @pytest.mark.anyio
    async def test_se_guardan_cifrados(self, contenedor, servicio, falso, base):
        """
        Son credenciales de otro sistema: un dump que las entregue en claro es un incidente en
        la API del tercero además del propio, y el usuario ni se enteraría.
        """
        estado, _ = await _flujo(servicio, falso)
        entrada = await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        from sqlalchemy import select

        from hexcore.darwin.infrastructure.models import AccountModel
        from hexcore.infrastructure.uow.scopes import session_scope

        async with session_scope() as sesion:
            filas = (
                await sesion.execute(
                    select(AccountModel).where(AccountModel.provider_id == "google")
                )
            ).scalars().all()

        fila = filas[0]
        assert "at-del-proveedor" not in (fila.access_token or "")
        assert "rt-del-proveedor" not in (fila.refresh_token or "")
        assert entrada.user.id == fila.user_id

    @pytest.mark.anyio
    async def test_se_pueden_descifrar_para_llamar_a_la_api(
        self, contenedor, servicio, falso
    ):
        estado, _ = await _flujo(servicio, falso)
        entrada = await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        cuentas = await contenedor.accounts().list_for_user(entrada.user.id)
        cuenta = next(c for c in cuentas if c.provider_id == "google")

        assert servicio.decrypt_access_token(cuenta) == "at-del-proveedor"
        assert servicio.decrypt_refresh_token(cuenta) == "rt-del-proveedor"

    @pytest.mark.anyio
    async def test_el_vencimiento_se_calcula_del_expires_in(
        self, contenedor, servicio, falso
    ):
        estado, _ = await _flujo(servicio, falso)
        entrada = await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        cuentas = await contenedor.accounts().list_for_user(entrada.user.id)
        cuenta = next(c for c in cuentas if c.provider_id == "google")

        assert cuenta.access_token_expires_at is not None
        assert (cuenta.access_token_expires_at - AHORA).total_seconds() == 3600

    @pytest.mark.anyio
    async def test_sin_refresh_token_no_explota(self, base, reloj):
        """GitHub no devuelve refresh token, y eso no es un error."""
        falso = ProveedorFalso(tokens=OAuthTokens(access_token="solo-at"))
        _, plugin = _armar(reloj, falso)
        try:
            servicio = plugin.service()
            estado, _ = await _flujo(servicio, falso)
            entrada = await servicio.callback(
                "google", code="c", state=estado, redirect_uri=CALLBACK
            )
            assert entrada.tokens.access_token
        finally:
            reset_identity()


# ── Desvincular ───────────────────────────────────────────────────────────────
class TestDesvincular:
    @pytest.mark.anyio
    async def test_no_deja_la_cuenta_sin_acceso(self, servicio, falso):
        """
        ⚠️ Desvincular el único proveedor de un usuario sin contraseña lo deja afuera de su
        propia cuenta, y ese botón está a un click en cualquier pantalla de ajustes.
        """
        estado, _ = await _flujo(servicio, falso)
        entrada = await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        with pytest.raises(ValueError, match="único método de acceso"):
            await servicio.unlink(user_id=entrada.user.id, provider_id="google")

        assert await servicio.list_linked(entrada.user.id) == ["google"]

    @pytest.mark.anyio
    async def test_con_contrasena_si_se_puede(self, contenedor, servicio, falso):
        usuario, _ = await contenedor.identity_service().sign_up(
            email="ana@ejemplo.com", password=PASS
        )
        estado, _ = await _flujo(servicio, falso, link_user_id=usuario.id)
        await servicio.callback(
            "google", code="c", state=estado, redirect_uri=CALLBACK
        )

        await servicio.unlink(user_id=usuario.id, provider_id="google")

        assert await servicio.list_linked(usuario.id) == []

    @pytest.mark.anyio
    async def test_desvincular_lo_que_no_esta_falla(self, contenedor, servicio):
        usuario, _ = await contenedor.identity_service().sign_up(
            email="ana@ejemplo.com", password=PASS
        )

        with pytest.raises(OAuthProviderNotConfiguredError):
            await servicio.unlink(user_id=usuario.id, provider_id="google")


# ── Errores del proveedor ─────────────────────────────────────────────────────
class TestErroresDelProveedor:
    @pytest.mark.anyio
    async def test_un_canje_rechazado_propaga(self, base, reloj):
        falso = ProveedorFalso(falla_canje=True)
        _, plugin = _armar(reloj, falso)
        try:
            servicio = plugin.service()
            estado, _ = await _flujo(servicio, falso)

            with pytest.raises(OAuthExchangeError):
                await servicio.callback(
                    "google", code="c", state=estado, redirect_uri=CALLBACK
                )
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_un_perfil_rechazado_propaga(self, base, reloj):
        falso = ProveedorFalso(falla_perfil=True)
        _, plugin = _armar(reloj, falso)
        try:
            servicio = plugin.service()
            estado, _ = await _flujo(servicio, falso)

            with pytest.raises(OAuthExchangeError):
                await servicio.callback(
                    "google", code="c", state=estado, redirect_uri=CALLBACK
                )
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_el_state_ya_se_consumio_aunque_el_proveedor_falle(self, base, reloj):
        """
        Deliberado: el `state` es un vale de un solo uso para *intentar* el flujo. Si un canje
        fallido lo dejara vivo, quien lo tenga podría reintentar indefinidamente.
        """
        falso = ProveedorFalso(falla_canje=True)
        _, plugin = _armar(reloj, falso)
        try:
            servicio = plugin.service()
            estado, _ = await _flujo(servicio, falso)

            with pytest.raises(OAuthExchangeError):
                await servicio.callback(
                    "google", code="c", state=estado, redirect_uri=CALLBACK
                )

            falso.falla_canje = False
            with pytest.raises(OAuthStateError):
                await servicio.callback(
                    "google", code="c", state=estado, redirect_uri=CALLBACK
                )
        finally:
            reset_identity()


# ── El plugin como plugin ─────────────────────────────────────────────────────
class TestPlugin:
    def test_aporta_su_mixin(self):
        from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

        plugin = OAuthPlugin(providers=[google(client_id="a", client_secret="b")])
        mixins = plugin.tables()

        assert list(mixins) == ["OAuthStateMixin"]
        assert not issubclass(mixins["OAuthStateMixin"], Base)

    def test_aporta_su_mapa_de_excepciones(self):
        mapa = OAuthPlugin().exception_status_map()

        assert mapa[OAuthProviderNotConfiguredError] == 404
        assert mapa[OAuthStateError] == 401
        assert mapa[OAuthExchangeError] == 502, "es una falla aguas arriba, no propia"
        assert mapa[OAuthAccountNotLinkedError] == 409

    def test_no_mapea_la_excepcion_base(self):
        assert OAuthError not in OAuthPlugin().exception_status_map()

    def test_el_paso_de_arranque_avisa_sin_allowlist(self, caplog):
        import logging

        plugin = OAuthPlugin(providers=[google(client_id="a", client_secret="b")])
        paso = plugin.startup_steps()[0]

        with caplog.at_level(logging.WARNING, logger="hexcore.darwin.oauth"):
            asyncio.run(paso())

        assert "allowlist" in caplog.text

    def test_el_paso_de_arranque_calla_con_allowlist(self, caplog):
        import logging

        plugin = OAuthPlugin(
            providers=[google(client_id="a", client_secret="b")],
            allowed_redirect_uris=[CALLBACK],
        )

        with caplog.at_level(logging.WARNING, logger="hexcore.darwin.oauth"):
            asyncio.run(plugin.startup_steps()[0]())

        assert caplog.text == ""

    def test_el_servicio_sin_registrar_falla_con_remediacion(self, reloj):
        reset_identity()
        configure_identity(
            IdentityConfig(secret_key=CLAVE),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        )
        try:
            with pytest.raises(RuntimeError) as excinfo:
                get_oauth_service()

            assert "OAuthPlugin" in str(excinfo.value)
        finally:
            reset_identity()

    def test_convive_con_two_factor_en_el_mismo_registro(self):
        """
        Los dos aportan tabla y mapa de excepciones, y ninguno choca: es lo que el registro
        valida.
        """
        from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

        registro = PluginRegistry([OAuthPlugin(), TwoFactorPlugin()])
        registro.validate()

        assert registro.names == ("two_factor", "oauth"), "por prioridad: 20 antes que 30"
        assert set(registro.tables()) == {"TwoFactorMixin", "OAuthStateMixin"}
        assert len(registro.exception_status_map()) == 10


# ── El borde HTTP ─────────────────────────────────────────────────────────────
@pytest.fixture
def cliente(base, reloj, falso):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hexcore.darwin import build_identity_router
    from hexcore.fastapi import AppFeatures, create_app

    contenedor, plugin = _armar(reloj, falso)
    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True, health=False),
        routers=[build_identity_router(), *plugin.routers()],
    )
    with TestClient(app) as cliente:
        yield cliente, contenedor, plugin
    reset_identity()


class TestHttp:
    def test_providers_lista_los_configurados(self, cliente):
        http, _, _ = cliente

        respuesta = http.get("/auth/oauth/providers")

        assert respuesta.status_code == 200
        assert respuesta.json() == {"providers": ["google"]}

    def test_start_devuelve_la_url(self, cliente):
        http, _, _ = cliente

        respuesta = http.get(
            "/auth/oauth/google/start", params={"redirect_uri": CALLBACK}
        )

        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        assert cuerpo["url"].startswith("https://accounts.google.com/")
        assert cuerpo["state"]

    def test_start_con_redirect_responde_302(self, cliente):
        http, _, _ = cliente

        respuesta = http.get(
            "/auth/oauth/google/start",
            params={"redirect_uri": CALLBACK, "redirect": True},
            follow_redirects=False,
        )

        assert respuesta.status_code == 302
        assert respuesta.headers["location"].startswith("https://accounts.google.com/")

    def test_un_proveedor_no_configurado_da_404(self, cliente):
        """404 y no 400: no le confirma a quien enumera cuáles sí están configurados."""
        http, _, _ = cliente

        respuesta = http.get(
            "/auth/oauth/github/start", params={"redirect_uri": CALLBACK}
        )

        assert respuesta.status_code == 404

    def test_el_callback_completo(self, cliente):
        http, _, _ = cliente

        inicio = http.get(
            "/auth/oauth/google/start", params={"redirect_uri": CALLBACK}
        ).json()

        respuesta = http.get(
            "/auth/oauth/google/callback",
            params={
                "code": "el-code",
                "state": inicio["state"],
                "redirect_uri": CALLBACK,
            },
            headers={"X-Darwin-Transport": "bearer"},
        )

        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["access_token"]
        assert cuerpo["created"] is True

    def test_un_state_malo_da_401(self, cliente):
        http, _, _ = cliente

        respuesta = http.get(
            "/auth/oauth/google/callback",
            params={"code": "c", "state": "inventado", "redirect_uri": CALLBACK},
            headers={"X-Darwin-Transport": "bearer"},
        )

        assert respuesta.status_code == 401

    def test_linked_sin_sesion_da_401(self, cliente):
        http, _, _ = cliente

        assert http.get("/auth/oauth/linked").status_code == 401

    def test_link_y_linked_con_sesion(self, cliente):
        http, contenedor, _ = cliente

        alta = http.post(
            "/auth/sign-up", json={"email": "ana@ejemplo.com", "password": PASS}
        )
        assert alta.status_code == 201
        entrada = http.post(
            "/auth/sign-in",
            json={"email": "ana@ejemplo.com", "password": PASS},
            headers={"X-Darwin-Transport": "bearer"},
        )
        auth = {
            "Authorization": f"Bearer {entrada.json()['access_token']}",
            "X-Darwin-Transport": "bearer",
        }

        inicio = http.get(
            "/auth/oauth/google/link",
            params={"redirect_uri": CALLBACK},
            headers=auth,
        )
        assert inicio.status_code == 200

        canje = http.get(
            "/auth/oauth/google/callback",
            params={
                "code": "c",
                "state": inicio.json()["state"],
                "redirect_uri": CALLBACK,
            },
            headers={"X-Darwin-Transport": "bearer"},
        )
        assert canje.status_code == 200
        assert canje.json()["created"] is False

        assert http.get("/auth/oauth/linked", headers=auth).json() == {
            "providers": ["google"]
        }

    def test_el_mail_coincidente_da_409_por_http(self, cliente):
        """El status del plugin, llegando al borde por `exception_status_map()`."""
        http, _, _ = cliente

        http.post("/auth/sign-up", json={"email": "ana@ejemplo.com", "password": PASS})
        inicio = http.get(
            "/auth/oauth/google/start", params={"redirect_uri": CALLBACK}
        ).json()

        respuesta = http.get(
            "/auth/oauth/google/callback",
            params={"code": "c", "state": inicio["state"], "redirect_uri": CALLBACK},
            headers={"X-Darwin-Transport": "bearer"},
        )

        assert respuesta.status_code == 409, respuesta.text
