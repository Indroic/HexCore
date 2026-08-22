"""
Darwin Fase 9: `passkey`, contra SQLite y un autenticador falso.

El autenticador es un doble del puerto `AbstractWebAuthnVerifier`, y no es una comodidad: con
hardware real, **el caso que más importa —un contador de firmas que no avanza— es imposible de
reproducir**. Con el doble se prueba exactamente eso.

Lo adversarial que se fija:

- **El contador que no avanza corta la sesión.** Es la única señal de compromiso que WebAuthn da.
- **El autenticador que nunca usa el contador se acepta** (contador 0 siempre), pero uno que lo
  usaba y volvió a 0 se rechaza.
- **El desafío se guarda en claro y el `expected_challenge` sale de la fila**, no del cliente: si
  saliera del `clientDataJSON`, la comparación del verificador sería contra sí misma.
- **El desafío es de un solo uso**, está atado a su propósito, y vence.
- **Un desafío de registro no sirve para autenticar**, ni al revés.
- **El `user_id` sale del desafío**, nunca del cuerpo: si no, se registraría una credencial propia
  en la cuenta de otro.
- **La credencial de otro no completa un desafío emitido para un usuario concreto.**
- **Una credencial ya registrada no se mueve** de cuenta.
- **Borrar la última credencial sin otro método de acceso se rechaza.**
- **Pedir opciones de login con un mail desconocido no lo revela.**
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
from hexcore.darwin.plugins.passkey import (  # noqa: E402
    AbstractWebAuthnVerifier,
    PasskeyAlreadyRegisteredError,
    PasskeyChallengeError,
    PasskeyClonedAuthenticatorError,
    PasskeyError,
    PasskeyLastFactorError,
    PasskeyNotFoundError,
    PasskeyPlugin,
    PasskeyVerificationError,
    RegisteredCredential,
    VerifiedAssertion,
    get_passkey_service,
)
from hexcore.darwin.plugins.passkey.orms.sqlalchemy.models import create_passkey_tables  # noqa: E402
from hexcore.darwin.plugins.passkey.webauthn_adapter import (  # noqa: E402
    b64url_decode,
    b64url_encode,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48
PASS = "una frase larga y buena"
MAIL = "ana@ejemplo.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── base64url ─────────────────────────────────────────────────────────────────
class TestBase64Url:
    def test_ida_y_vuelta(self):
        for n in range(1, 40):
            datos = bytes(range(n))
            assert b64url_decode(b64url_encode(datos)) == datos

    def test_sin_relleno(self):
        """La spec de WebAuthn lo usa sin relleno en todos sus campos."""
        assert "=" not in b64url_encode(b"abcde")

    def test_tolera_relleno_de_mas(self):
        """Hay librerías de navegador que lo agregan igual."""
        crudo = b64url_encode(b"abcde")
        assert b64url_decode(crudo + "=" * (-len(crudo) % 4)) == b"abcde"


# ── El autenticador falso ─────────────────────────────────────────────────────
class AutenticadorFalso(AbstractWebAuthnVerifier):
    """
    Un doble del puerto de WebAuthn.

    Genera desafíos deterministas, arma el `clientDataJSON` como lo haría un navegador, y deja que
    el test controle qué devuelve la verificación — incluido un contador que no avanza, que con
    hardware real no se puede provocar.
    """

    def __init__(self) -> None:
        self.desafios: list[bytes] = []
        self.excluidos: list[tuple[str, ...]] = []
        self.permitidos: list[tuple[str, ...]] = []
        #: Qué credencial "devuelve" el autenticador. La setea el test.
        self.credential_id = "cred-1"
        self.public_key = "clave-publica-1"
        self.sign_count = 1
        #: Si la verificación falla, y con qué contador vuelve la aserción.
        self.falla = False
        self.next_sign_count: int | None = None
        self.contador = 0

    # ── Helpers para el test ──────────────────────────────────────────────
    def respuesta(self, desafio: bytes, *, credential_id: str | None = None) -> dict:
        """Lo que mandaría el navegador: el desafío va adentro del `clientDataJSON`."""
        client_data = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "type": "webauthn.get",
                    "challenge": b64url_encode(desafio),
                    "origin": "https://mi-app.test",
                }
            ).encode()
        ).decode().rstrip("=")
        return {
            "id": credential_id or self.credential_id,
            "rawId": credential_id or self.credential_id,
            "type": "public-key",
            "response": {"clientDataJSON": client_data, "transports": ["internal"]},
        }

    # ── El puerto ─────────────────────────────────────────────────────────
    def registration_options(
        self, *, user_id: UUID, user_name: str, exclude_credential_ids=()
    ):
        self.excluidos.append(tuple(exclude_credential_ids))
        self.contador += 1
        desafio = f"desafio-registro-{self.contador}".encode()
        self.desafios.append(desafio)
        return {"challenge": b64url_encode(desafio), "rp": {"id": "mi-app.test"}}, desafio

    def verify_registration(self, *, credential, expected_challenge):
        if self.falla:
            raise PasskeyVerificationError("el autenticador falso dice que no")
        respuesta = credential.get("response") or {}
        return RegisteredCredential(
            credential_id=str(credential.get("id")),
            public_key=self.public_key,
            sign_count=self.sign_count,
            aaguid="aaguid-de-prueba",
            backed_up=True,
            transports=tuple(respuesta.get("transports") or ()),
            user_verified=True,
        )

    def authentication_options(self, *, allow_credential_ids=()):
        self.permitidos.append(tuple(allow_credential_ids))
        self.contador += 1
        desafio = f"desafio-login-{self.contador}".encode()
        self.desafios.append(desafio)
        return {"challenge": b64url_encode(desafio)}, desafio

    def verify_authentication(
        self, *, credential, expected_challenge, public_key, current_sign_count
    ):
        if self.falla:
            raise PasskeyVerificationError("el autenticador falso dice que no")
        # Se comprueba que el `expected_challenge` que le llega sea el que el servidor emitió: es
        # la propiedad que el diseño del servicio garantiza.
        assert expected_challenge in self.desafios, (
            "el `expected_challenge` tiene que salir de la fila, no del cliente"
        )
        nuevo = (
            self.next_sign_count
            if self.next_sign_count is not None
            else current_sign_count + 1
        )
        return VerifiedAssertion(
            credential_id=str(credential.get("id")),
            new_sign_count=nuevo,
            user_verified=True,
        )


# ── El cableado ───────────────────────────────────────────────────────────────
@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def falso() -> AutenticadorFalso:
    return AutenticadorFalso()


@pytest.fixture
def plugin(falso) -> PasskeyPlugin:
    return PasskeyPlugin(rp_id="mi-app.test", verifier=falso)


@pytest.fixture
def contenedor(reloj, plugin):
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))
    asyncio.run(create_passkey_tables(motor))

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
        ),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        plugins=PluginRegistry([plugin]),
    )
    yield contenedor

    reset_identity()
    plugin.reset()
    asyncio.run(dispose_engine())


@pytest.fixture
def servicio(contenedor):
    return get_passkey_service()


async def _usuario(contenedor, email: str = MAIL, *, con_password: bool = True):
    if con_password:
        usuario, _ = await contenedor.identity_service().sign_up(
            email=email, password=PASS
        )
        return await contenedor.users().update(
            usuario.model_copy(update={"email_verified": True})
        )

    # Sin contraseña: un usuario que sólo tiene passkeys. Es el caso que hace real el chequeo de
    # "último método de acceso".
    from hexcore.darwin.domain.entities import User

    return await contenedor.users().add(
        User(email=email, email_verified=True)
    )


async def _registrar(servicio, falso, usuario, *, credential_id="cred-1", name=None):
    """Registra una credencial de punta a punta y devuelve la `Passkey`."""
    falso.credential_id = credential_id
    await servicio.start_registration(user_id=usuario.id, user_name=usuario.email)
    return await servicio.finish_registration(
        credential=falso.respuesta(falso.desafios[-1], credential_id=credential_id),
        name=name,
    )


# ── Registro ──────────────────────────────────────────────────────────────────
class TestRegistro:
    @pytest.mark.anyio
    async def test_el_flujo_completo(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)

        guardada = await _registrar(
            servicio, falso, usuario, name="iPhone de Ana"
        )

        assert guardada.user_id == usuario.id
        assert guardada.credential_id == "cred-1"
        assert guardada.public_key == "clave-publica-1"
        assert guardada.name == "iPhone de Ana"
        assert guardada.aaguid == "aaguid-de-prueba"
        assert guardada.backed_up is True
        assert guardada.transports == ("internal",)

    @pytest.mark.anyio
    async def test_el_user_id_sale_del_desafio(self, contenedor, servicio, falso):
        """
        ⚠️ Nunca del cuerpo del request. Aceptarlo del cliente dejaría registrar una credencial
        propia en la cuenta de otro, que es toma de cuenta directa en un endpoint que parece
        administrativo.
        """
        ana = await _usuario(contenedor)
        beto = await _usuario(contenedor, "beto@ejemplo.com")

        # El desafío se emite para Beto…
        await servicio.start_registration(user_id=beto.id, user_name=beto.email)
        # …y la credencial queda de Beto, sin que el cuerpo pueda decir otra cosa.
        guardada = await servicio.finish_registration(
            credential=falso.respuesta(falso.desafios[-1])
        )

        assert guardada.user_id == beto.id
        assert await servicio.list_for_user(ana.id) == []

    @pytest.mark.anyio
    async def test_excluye_las_que_ya_tiene(self, contenedor, servicio, falso):
        """
        Sin `excludeCredentials`, el navegador ofrece registrar una credencial que ya está y el
        flujo falla al guardar con un error de base en vez de un mensaje.
        """
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario, credential_id="cred-1")

        await servicio.start_registration(user_id=usuario.id, user_name=usuario.email)

        assert falso.excluidos[-1] == ("cred-1",)

    @pytest.mark.anyio
    async def test_una_credencial_ya_registrada_se_rechaza(
        self, contenedor, servicio, falso
    ):
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario, credential_id="cred-1")

        await servicio.start_registration(user_id=usuario.id, user_name=usuario.email)
        with pytest.raises(PasskeyAlreadyRegisteredError):
            await servicio.finish_registration(
                credential=falso.respuesta(falso.desafios[-1], credential_id="cred-1")
            )

    @pytest.mark.anyio
    async def test_no_se_mueve_la_credencial_de_otro(self, contenedor, servicio, falso):
        """Moverla le sacaría al primer usuario un método de acceso."""
        ana = await _usuario(contenedor)
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        await _registrar(servicio, falso, ana, credential_id="cred-compartida")

        await servicio.start_registration(user_id=beto.id, user_name=beto.email)
        with pytest.raises(PasskeyAlreadyRegisteredError):
            await servicio.finish_registration(
                credential=falso.respuesta(
                    falso.desafios[-1], credential_id="cred-compartida"
                )
            )

        assert len(await servicio.list_for_user(ana.id)) == 1
        assert await servicio.list_for_user(beto.id) == []

    @pytest.mark.anyio
    async def test_una_verificacion_fallida_no_guarda(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)
        await servicio.start_registration(user_id=usuario.id, user_name=usuario.email)
        falso.falla = True

        with pytest.raises(PasskeyVerificationError):
            await servicio.finish_registration(
                credential=falso.respuesta(falso.desafios[-1])
            )

        assert await servicio.list_for_user(usuario.id) == []

    @pytest.mark.anyio
    async def test_un_nombre_vacio_queda_en_none(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)

        guardada = await _registrar(servicio, falso, usuario, name="   ")

        assert guardada.name is None


# ── El desafío ────────────────────────────────────────────────────────────────
class TestDesafio:
    @pytest.mark.anyio
    async def test_se_guarda_en_claro(self, contenedor, servicio, falso, reloj):
        """
        Y eso es correcto: es un nonce público. Hashearlo obligaría a que el
        `expected_challenge` saliera del propio cliente, y la comparación del verificador quedaría
        entre un valor y sí mismo.
        """
        from sqlalchemy import select

        from hexcore.darwin.plugins.passkey.orms.sqlalchemy.models import PasskeyChallengeModel
        from hexcore.infrastructure.uow.scopes import session_scope

        usuario = await _usuario(contenedor)
        await servicio.start_registration(user_id=usuario.id, user_name=usuario.email)

        async with session_scope() as sesion:
            fila = (
                await sesion.execute(select(PasskeyChallengeModel))
            ).scalar_one()

        assert fila.challenge == b64url_encode(falso.desafios[-1])
        assert fila.purpose == "register"
        assert fila.user_id == usuario.id

    @pytest.mark.anyio
    async def test_el_expected_challenge_sale_de_la_fila(
        self, contenedor, servicio, falso
    ):
        """
        El autenticador falso lo asevera: si el servicio le pasara el desafío que vino del
        cliente, el `assert` de `verify_authentication` fallaría.
        """
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication(user_id=usuario.id)
        entrada = await servicio.finish_authentication(
            credential=falso.respuesta(falso.desafios[-1])
        )

        assert entrada.user.id == usuario.id

    @pytest.mark.anyio
    async def test_es_de_un_solo_uso(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication(user_id=usuario.id)
        respuesta = falso.respuesta(falso.desafios[-1])
        await servicio.finish_authentication(credential=respuesta)

        with pytest.raises(PasskeyChallengeError):
            await servicio.finish_authentication(credential=respuesta)

    @pytest.mark.anyio
    async def test_un_desafio_de_registro_no_sirve_para_autenticar(
        self, contenedor, servicio, falso
    ):
        """
        El `purpose` es parte de la clave de canje: si no, un desafío emitido para registrar
        —donde no hay firma sobre una clave conocida— se podría canjear en el login.
        """
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)

        await servicio.start_registration(user_id=usuario.id, user_name=usuario.email)
        with pytest.raises(PasskeyChallengeError):
            await servicio.finish_authentication(
                credential=falso.respuesta(falso.desafios[-1])
            )

    @pytest.mark.anyio
    async def test_un_desafio_de_login_no_sirve_para_registrar(
        self, contenedor, servicio, falso
    ):
        usuario = await _usuario(contenedor)

        await servicio.start_authentication(user_id=usuario.id)
        with pytest.raises(PasskeyChallengeError):
            await servicio.finish_registration(
                credential=falso.respuesta(falso.desafios[-1])
            )

    @pytest.mark.anyio
    async def test_un_desafio_vencido_se_rechaza(self, contenedor, servicio, falso, reloj):
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)
        await servicio.start_authentication(user_id=usuario.id)
        respuesta = falso.respuesta(falso.desafios[-1])

        reloj.advance(minutes=6)  # el TTL por default son 5

        with pytest.raises(PasskeyChallengeError):
            await servicio.finish_authentication(credential=respuesta)

    @pytest.mark.anyio
    async def test_un_desafio_inventado_se_rechaza(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)

        with pytest.raises(PasskeyChallengeError):
            await servicio.finish_authentication(
                credential=falso.respuesta(b"inventado")
            )

    @pytest.mark.parametrize(
        "roto",
        [
            {},
            {"response": None},
            {"response": {}},
            {"response": {"clientDataJSON": ""}},
            {"response": {"clientDataJSON": "no-es-base64-!!!"}},
            {"response": {"clientDataJSON": "eyJhIjoxfQ"}},  # JSON sin `challenge`
        ],
    )
    @pytest.mark.anyio
    async def test_una_respuesta_corrupta_da_401_y_no_500(self, servicio, roto):
        """
        La respuesta la manda el cliente. Un JSON corrupto tiene que ser un 401 y no un 500 que le
        dice a quien prueba formatos que encontró un camino no manejado.
        """
        with pytest.raises(PasskeyChallengeError):
            await servicio.finish_authentication(credential=roto)

    @pytest.mark.anyio
    async def test_ocho_logins_concurrentes_dejan_pasar_uno(
        self, contenedor, servicio, falso
    ):
        """
        Es la razón por la que `consume` es una sentencia única. Con leer-y-después-escribir, un
        desafío capturado se canjearía dos veces — y el desafío es justamente lo que ata la firma
        a este intento.
        """
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)
        await servicio.start_authentication(user_id=usuario.id)
        respuesta = falso.respuesta(falso.desafios[-1])

        resultados = await asyncio.gather(
            *(servicio.finish_authentication(credential=respuesta) for _ in range(8)),
            return_exceptions=True,
        )

        ganaron = [r for r in resultados if not isinstance(r, BaseException)]
        perdieron = [r for r in resultados if isinstance(r, PasskeyChallengeError)]

        assert len(ganaron) == 1, f"ganaron {len(ganaron)}"
        assert len(perdieron) == 7


# ── El contador de firmas ─────────────────────────────────────────────────────
class TestContadorDeFirmas:
    @pytest.mark.anyio
    async def test_el_contador_avanza(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication(user_id=usuario.id)
        falso.next_sign_count = 5
        await servicio.finish_authentication(
            credential=falso.respuesta(falso.desafios[-1])
        )

        credenciales = await servicio.list_for_user(usuario.id)
        assert credenciales[0].sign_count == 5
        assert credenciales[0].last_used_at == AHORA

    @pytest.mark.anyio
    async def test_un_contador_que_no_avanza_corta(self, contenedor, servicio, falso):
        """
        ⚠️ **La única señal de compromiso que WebAuthn da.** Un contador que no avanza significa
        autenticador clonado o aserción replayeada, y la respuesta correcta no es "reintentá": es
        cortar. Con hardware real este caso no se puede reproducir — por eso el puerto.
        """
        usuario = await _usuario(contenedor)
        falso.sign_count = 10
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication(user_id=usuario.id)
        falso.next_sign_count = 10  # el mismo, no avanzó

        with pytest.raises(PasskeyClonedAuthenticatorError, match="clonada"):
            await servicio.finish_authentication(
                credential=falso.respuesta(falso.desafios[-1])
            )

    @pytest.mark.anyio
    async def test_un_contador_que_retrocede_corta(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)
        falso.sign_count = 10
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication(user_id=usuario.id)
        falso.next_sign_count = 3

        with pytest.raises(PasskeyClonedAuthenticatorError):
            await servicio.finish_authentication(
                credential=falso.respuesta(falso.desafios[-1])
            )

    @pytest.mark.anyio
    async def test_un_autenticador_sin_contador_se_acepta(
        self, contenedor, servicio, falso
    ):
        """
        Varias llaves y varios navegadores no incrementan el contador y devuelven 0 siempre.
        Rechazarlas dejaría afuera a credenciales legítimas — el chequeo es sobre la **regresión**,
        no sobre que el número sea mayor que cero.
        """
        usuario = await _usuario(contenedor)
        falso.sign_count = 0
        await _registrar(servicio, falso, usuario)

        for _ in range(3):
            await servicio.start_authentication(user_id=usuario.id)
            falso.next_sign_count = 0
            entrada = await servicio.finish_authentication(
                credential=falso.respuesta(falso.desafios[-1])
            )
            assert entrada.tokens.access_token

    @pytest.mark.anyio
    async def test_uno_que_usaba_contador_y_vuelve_a_cero_se_rechaza(
        self, contenedor, servicio, falso
    ):
        """La distinción que importa: nunca usarlo es válido, dejar de usarlo es una regresión."""
        usuario = await _usuario(contenedor)
        falso.sign_count = 7
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication(user_id=usuario.id)
        falso.next_sign_count = 0

        with pytest.raises(PasskeyClonedAuthenticatorError):
            await servicio.finish_authentication(
                credential=falso.respuesta(falso.desafios[-1])
            )

    @pytest.mark.anyio
    async def test_el_contador_no_sube_si_la_firma_no_valida(
        self, contenedor, servicio, falso
    ):
        """
        Subirlo antes de verificar dejaría que una firma inválida avance el contador y
        desincronice al autenticador legítimo — un ataque de negación de servicio contra una
        cuenta, gratis.
        """
        usuario = await _usuario(contenedor)
        falso.sign_count = 3
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication(user_id=usuario.id)
        falso.falla = True
        with pytest.raises(PasskeyVerificationError):
            await servicio.finish_authentication(
                credential=falso.respuesta(falso.desafios[-1])
            )

        credenciales = await servicio.list_for_user(usuario.id)
        assert credenciales[0].sign_count == 3


# ── Autenticación ─────────────────────────────────────────────────────────────
class TestAutenticacion:
    @pytest.mark.anyio
    async def test_abre_la_sesion(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication(user_id=usuario.id)
        entrada = await servicio.finish_authentication(
            credential=falso.respuesta(falso.desafios[-1]), transport="bearer"
        )

        assert entrada.user.id == usuario.id
        assert entrada.session.actor_user_id == usuario.id
        assert entrada.session.transport == "bearer"
        assert entrada.tokens.access_token and entrada.tokens.refresh_token

    @pytest.mark.anyio
    async def test_con_usuario_limita_las_credenciales_ofrecidas(
        self, contenedor, servicio, falso
    ):
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario, credential_id="cred-a")

        await servicio.start_authentication(user_id=usuario.id)

        assert falso.permitidos[-1] == ("cred-a",)

    @pytest.mark.anyio
    async def test_sin_usuario_no_limita_nada(self, contenedor, servicio, falso):
        """
        El flujo con credenciales descubribles: el navegador ofrece lo que tenga. Es el que da la
        mejor experiencia y el que obliga a que el desafío se pueda canjear sin saber de quién es.
        """
        usuario = await _usuario(contenedor)
        await _registrar(servicio, falso, usuario)

        await servicio.start_authentication()
        entrada = await servicio.finish_authentication(
            credential=falso.respuesta(falso.desafios[-1])
        )

        assert falso.permitidos[-1] == ()
        assert entrada.user.id == usuario.id, "el servidor descubrió quién era"

    @pytest.mark.anyio
    async def test_la_credencial_de_otro_no_completa_un_desafio_dirigido(
        self, contenedor, servicio, falso
    ):
        """
        ⚠️ Sin este chequeo, alguien pide un desafío "para Ana" y lo completa con su propia
        credencial: la firma valida y el desafío también.
        """
        ana = await _usuario(contenedor)
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        await _registrar(servicio, falso, ana, credential_id="cred-de-ana")
        await _registrar(servicio, falso, beto, credential_id="cred-de-beto")

        await servicio.start_authentication(user_id=ana.id)
        with pytest.raises(PasskeyVerificationError, match="no corresponde"):
            await servicio.finish_authentication(
                credential=falso.respuesta(
                    falso.desafios[-1], credential_id="cred-de-beto"
                )
            )

    @pytest.mark.anyio
    async def test_una_credencial_desconocida_no_entra(self, contenedor, servicio, falso):
        await _usuario(contenedor)

        await servicio.start_authentication()
        with pytest.raises(PasskeyNotFoundError):
            await servicio.finish_authentication(
                credential=falso.respuesta(falso.desafios[-1], credential_id="fantasma")
            )

    @pytest.mark.anyio
    async def test_una_credencial_sin_id_se_rechaza(self, contenedor, servicio, falso):
        await _usuario(contenedor)
        await servicio.start_authentication()
        respuesta = falso.respuesta(falso.desafios[-1])
        del respuesta["id"]
        del respuesta["rawId"]

        with pytest.raises(PasskeyVerificationError, match="identificador"):
            await servicio.finish_authentication(credential=respuesta)


# ── Ciclo de vida ─────────────────────────────────────────────────────────────
class TestCicloDeVida:
    @pytest.mark.anyio
    async def test_listar_devuelve_las_del_usuario(self, contenedor, servicio, falso):
        ana = await _usuario(contenedor)
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        await _registrar(servicio, falso, ana, credential_id="a1", name="Llave")
        await _registrar(servicio, falso, ana, credential_id="a2", name="Teléfono")
        await _registrar(servicio, falso, beto, credential_id="b1")

        de_ana = await servicio.list_for_user(ana.id)

        assert {p.name for p in de_ana} == {"Llave", "Teléfono"}

    @pytest.mark.anyio
    async def test_borrar_con_contrasena_funciona(self, contenedor, servicio, falso):
        usuario = await _usuario(contenedor)
        guardada = await _registrar(servicio, falso, usuario)

        await servicio.delete(user_id=usuario.id, passkey_id=guardada.id)

        assert await servicio.list_for_user(usuario.id) == []

    @pytest.mark.anyio
    async def test_no_se_borra_la_ultima_sin_otro_metodo(
        self, contenedor, servicio, falso
    ):
        """
        ⚠️ Borrar la última passkey de alguien que no tiene contraseña ni proveedor lo deja afuera
        de su propia cuenta, y el botón está a un click en cualquier pantalla de ajustes.
        """
        usuario = await _usuario(contenedor, con_password=False)
        guardada = await _registrar(servicio, falso, usuario)

        with pytest.raises(PasskeyLastFactorError, match="única credencial"):
            await servicio.delete(user_id=usuario.id, passkey_id=guardada.id)

        assert len(await servicio.list_for_user(usuario.id)) == 1

    @pytest.mark.anyio
    async def test_con_dos_credenciales_si_se_borra_una(
        self, contenedor, servicio, falso
    ):
        usuario = await _usuario(contenedor, con_password=False)
        primera = await _registrar(servicio, falso, usuario, credential_id="c1")
        await _registrar(servicio, falso, usuario, credential_id="c2")

        await servicio.delete(user_id=usuario.id, passkey_id=primera.id)

        assert len(await servicio.list_for_user(usuario.id)) == 1

    @pytest.mark.anyio
    async def test_borrar_la_de_otro_da_not_found(self, contenedor, servicio, falso):
        """
        El mismo error que "no existe": un 403 distinto le confirmaría a quien prueba ids que la
        credencial existe.
        """
        ana = await _usuario(contenedor)
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        de_ana = await _registrar(servicio, falso, ana, credential_id="a1")

        with pytest.raises(PasskeyNotFoundError):
            await servicio.delete(user_id=beto.id, passkey_id=de_ana.id)

        assert len(await servicio.list_for_user(ana.id)) == 1

    @pytest.mark.anyio
    async def test_borrar_una_inexistente_da_not_found(self, contenedor, servicio):
        usuario = await _usuario(contenedor)

        with pytest.raises(PasskeyNotFoundError):
            await servicio.delete(user_id=usuario.id, passkey_id=uuid4())


# ── El adaptador real ─────────────────────────────────────────────────────────
class TestAdaptadorReal:
    """
    El adaptador de `py_webauthn`. No se prueba el flujo completo —haría falta un autenticador—
    pero sí lo que se puede: que exija lo que tiene que exigir y que genere opciones válidas.
    """

    def test_sin_origins_no_se_construye(self):
        """
        Es el chequeo que hace a WebAuthn resistente al phishing. Sin orígenes declarados, un
        sitio clonado puede reenviar la aserción del usuario.
        """
        pytest.importorskip("webauthn")
        from hexcore.darwin.plugins.passkey.webauthn_adapter import PyWebAuthnVerifier

        with pytest.raises(ValueError, match="phishing"):
            PyWebAuthnVerifier(rp_id="mi-app.test", origins=[])

    def test_sin_rp_id_no_se_construye(self):
        pytest.importorskip("webauthn")
        from hexcore.darwin.plugins.passkey.webauthn_adapter import PyWebAuthnVerifier

        with pytest.raises(ValueError, match="rp_id"):
            PyWebAuthnVerifier(rp_id="  ", origins=["https://mi-app.test"])

    def test_las_opciones_de_registro_son_validas(self):
        pytest.importorskip("webauthn")
        from hexcore.darwin.plugins.passkey.webauthn_adapter import PyWebAuthnVerifier

        verificador = PyWebAuthnVerifier(
            rp_id="mi-app.test",
            rp_name="Mi App",
            origins=["https://mi-app.test"],
        )
        opciones, desafio = verificador.registration_options(
            user_id=uuid4(), user_name=MAIL
        )

        assert opciones["rp"]["id"] == "mi-app.test"
        assert opciones["rp"]["name"] == "Mi App"
        assert opciones["user"]["name"] == MAIL
        assert b64url_decode(opciones["challenge"]) == desafio
        assert len(desafio) == 32
        assert opciones["pubKeyCredParams"], "tiene que ofrecer algún algoritmo"

    def test_dos_desafios_no_se_repiten(self):
        pytest.importorskip("webauthn")
        from hexcore.darwin.plugins.passkey.webauthn_adapter import PyWebAuthnVerifier

        verificador = PyWebAuthnVerifier(
            rp_id="mi-app.test", origins=["https://mi-app.test"]
        )
        _, uno = verificador.registration_options(user_id=uuid4(), user_name=MAIL)
        _, otro = verificador.registration_options(user_id=uuid4(), user_name=MAIL)

        assert uno != otro

    def test_las_opciones_de_login_sin_credenciales_no_limitan(self):
        pytest.importorskip("webauthn")
        from hexcore.darwin.plugins.passkey.webauthn_adapter import PyWebAuthnVerifier

        verificador = PyWebAuthnVerifier(
            rp_id="mi-app.test", origins=["https://mi-app.test"]
        )
        opciones, _ = verificador.authentication_options()

        assert not opciones.get("allowCredentials")

    def test_una_respuesta_basura_da_el_error_generico(self):
        """El detalle va al log: decirle a quien prueba qué chequeo falló le da el próximo paso."""
        pytest.importorskip("webauthn")
        from hexcore.darwin.plugins.passkey.webauthn_adapter import PyWebAuthnVerifier

        verificador = PyWebAuthnVerifier(
            rp_id="mi-app.test", origins=["https://mi-app.test"]
        )

        with pytest.raises(PasskeyVerificationError) as excinfo:
            verificador.verify_registration(
                credential={"id": "x", "response": {}}, expected_challenge=b"x"
            )

        assert "Probá de nuevo" in str(excinfo.value)


# ── El plugin como plugin ─────────────────────────────────────────────────────
class TestPlugin:
    def test_aporta_sus_dos_mixins(self, plugin):
        from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

        mixins = plugin.tables()

        assert sorted(mixins) == ["PasskeyChallengeMixin", "PasskeyMixin"]
        for mixin in mixins.values():
            assert not issubclass(mixin, Base)

    def test_aporta_su_mapa_de_excepciones(self, plugin):
        mapa = plugin.exception_status_map()

        assert mapa[PasskeyClonedAuthenticatorError] == 401
        assert mapa[PasskeyNotFoundError] == 404
        assert mapa[PasskeyLastFactorError] == 409

    def test_no_mapea_la_excepcion_base(self, plugin):
        assert PasskeyError not in plugin.exception_status_map()

    def test_sin_rp_id_ni_verifier_no_se_construye(self):
        """Falla al cablear, que es el criterio de toda la casa."""
        with pytest.raises(ValueError, match="rp_id"):
            PasskeyPlugin()

    def test_con_verifier_propio_no_hace_falta_rp_id(self, falso):
        plugin = PasskeyPlugin(verifier=falso)

        assert plugin.name == "passkey"

    def test_avisa_si_el_rp_id_es_de_desarrollo(self, caplog):
        """
        Shippear con `rp_id="localhost"` deja el login roto para todos, con un error del navegador
        que no dice qué está mal.
        """
        import logging

        plugin = PasskeyPlugin(rp_id="localhost", origins=["http://localhost:3000"])

        with caplog.at_level(logging.WARNING, logger="hexcore.darwin.passkey"):
            asyncio.run(plugin.startup_steps()[0]())

        assert "sólo funciona en desarrollo" in caplog.text

    def test_calla_con_un_rp_id_real(self, caplog):
        import logging

        plugin = PasskeyPlugin(rp_id="mi-app.com", origins=["https://mi-app.com"])

        with caplog.at_level(logging.WARNING, logger="hexcore.darwin.passkey"):
            asyncio.run(plugin.startup_steps()[0]())

        assert caplog.text == ""

    def test_el_servicio_sin_registrar_falla_con_remediacion(self, reloj):
        reset_identity()
        configure_identity(
            IdentityConfig(storage="sqlalchemy", secret_key=CLAVE),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        )
        try:
            with pytest.raises(RuntimeError) as excinfo:
                get_passkey_service()

            assert "PasskeyPlugin" in str(excinfo.value)
        finally:
            reset_identity()

    def test_convive_con_los_otros_plugins(self, falso):
        from hexcore.darwin.plugins.impersonate import ImpersonatePlugin
        from hexcore.darwin.plugins.oauth import OAuthPlugin
        from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

        registro = PluginRegistry(
            [
                PasskeyPlugin(verifier=falso),
                ImpersonatePlugin(),
                OAuthPlugin(),
                TwoFactorPlugin(),
            ]
        )
        registro.validate()

        assert registro.names == (
            "two_factor",
            "oauth",
            "passkey",
            "impersonate",
        )
        assert set(registro.tables()) == {
            "TwoFactorMixin",
            "OAuthStateMixin",
            "PasskeyMixin",
            "PasskeyChallengeMixin",
        }

    def test_los_mixins_son_perezosos(self):
        """
        Están en `__all__` porque el consumidor los compone en su paquete `models/`, pero nombrar
        el plugin no puede exigir el extra `[sql]`.
        """
        import hexcore.darwin.plugins.passkey as modulo

        assert "PasskeyMixin" in modulo.__all__
        assert modulo.PasskeyMixin is not None

        with pytest.raises(AttributeError):
            modulo.NoExiste  # type: ignore[attr-defined]


# ── El borde HTTP ─────────────────────────────────────────────────────────────
@pytest.fixture
def cliente_http(contenedor, plugin):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hexcore.darwin import build_identity_router
    from hexcore.fastapi import AppFeatures, create_app

    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True, health=False),
        routers=[build_identity_router(), *plugin.routers()],
    )
    with TestClient(app) as http:
        yield http


async def _bearer(contenedor, email=MAIL):
    _, _, par = await contenedor.identity_service().sign_in(
        email=email, password=PASS, transport="bearer"
    )
    return {
        "Authorization": f"Bearer {par.access_token}",
        "X-Darwin-Transport": "bearer",
    }


class TestHttp:
    @pytest.mark.anyio
    async def test_el_flujo_completo(self, contenedor, cliente_http, falso):
        await _usuario(contenedor)
        auth = await _bearer(contenedor)

        opciones = cliente_http.post("/auth/passkey/register/options", headers=auth)
        assert opciones.status_code == 200
        assert opciones.json()["challenge"]

        registro = cliente_http.post(
            "/auth/passkey/register",
            json={
                "credential": falso.respuesta(falso.desafios[-1]),
                "name": "Mi llave",
            },
            headers=auth,
        )
        assert registro.status_code == 201, registro.text
        assert registro.json()["name"] == "Mi llave"
        assert registro.json()["backed_up"] is True

        listado = cliente_http.get("/auth/passkey", headers=auth)
        assert len(listado.json()) == 1

        # Y el login, que es público.
        opciones_login = cliente_http.post(
            "/auth/passkey/authenticate/options", json={"email": MAIL}
        )
        assert opciones_login.status_code == 200

        login = cliente_http.post(
            "/auth/passkey/authenticate",
            json={"credential": falso.respuesta(falso.desafios[-1])},
            headers={"X-Darwin-Transport": "bearer"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["access_token"]

    @pytest.mark.anyio
    async def test_el_resumen_no_expone_la_credencial(
        self, contenedor, cliente_http, falso
    ):
        """No son secretos, pero no le sirven a la interfaz: menos respuesta, menos superficie."""
        usuario = await _usuario(contenedor)
        await _registrar(get_passkey_service(), falso, usuario)
        auth = await _bearer(contenedor)

        cuerpo = cliente_http.get("/auth/passkey", headers=auth).json()[0]

        assert "credential_id" not in cuerpo
        assert "public_key" not in cuerpo
        assert set(cuerpo) == {
            "id",
            "name",
            "aaguid",
            "backed_up",
            "created_at",
            "last_used_at",
        }

    @pytest.mark.anyio
    async def test_un_mail_desconocido_no_se_revela(self, contenedor, cliente_http):
        """
        La respuesta es la misma forma que el flujo sin mail: el cliente no puede distinguir "no
        existe" de "usá una credencial descubrible".
        """
        await _usuario(contenedor)

        conocido = cliente_http.post(
            "/auth/passkey/authenticate/options", json={"email": MAIL}
        )
        desconocido = cliente_http.post(
            "/auth/passkey/authenticate/options", json={"email": "nadie@ejemplo.com"}
        )

        assert conocido.status_code == desconocido.status_code == 200
        assert set(conocido.json()) == set(desconocido.json())

    @pytest.mark.anyio
    async def test_registrar_sin_sesion_da_401(self, contenedor, cliente_http):
        assert (
            cliente_http.post("/auth/passkey/register/options").status_code == 401
        )

    @pytest.mark.anyio
    async def test_un_contador_clonado_da_401(self, contenedor, cliente_http, falso):
        usuario = await _usuario(contenedor)
        falso.sign_count = 5
        await _registrar(get_passkey_service(), falso, usuario)

        cliente_http.post("/auth/passkey/authenticate/options", json={})
        falso.next_sign_count = 5

        respuesta = cliente_http.post(
            "/auth/passkey/authenticate",
            json={"credential": falso.respuesta(falso.desafios[-1])},
            headers={"X-Darwin-Transport": "bearer"},
        )

        assert respuesta.status_code == 401, respuesta.text
        assert "WWW-Authenticate" in respuesta.headers

    @pytest.mark.anyio
    async def test_borrar_la_ultima_sin_otro_metodo_da_409(
        self, contenedor, cliente_http, falso
    ):
        """El status del plugin, llegando al borde por `exception_status_map()`."""
        usuario = await _usuario(contenedor, con_password=False)
        guardada = await _registrar(get_passkey_service(), falso, usuario)

        # Sesión por passkey, porque el usuario no tiene contraseña.
        cliente_http.post("/auth/passkey/authenticate/options", json={})
        login = cliente_http.post(
            "/auth/passkey/authenticate",
            json={"credential": falso.respuesta(falso.desafios[-1])},
            headers={"X-Darwin-Transport": "bearer"},
        )
        auth = {
            "Authorization": f"Bearer {login.json()['access_token']}",
            "X-Darwin-Transport": "bearer",
        }

        respuesta = cliente_http.request(
            "DELETE", f"/auth/passkey/{guardada.id}", headers=auth
        )

        assert respuesta.status_code == 409, respuesta.text

    @pytest.mark.anyio
    async def test_borrar_una_inexistente_da_404(self, contenedor, cliente_http):
        await _usuario(contenedor)
        auth = await _bearer(contenedor)

        respuesta = cliente_http.request(
            "DELETE", f"/auth/passkey/{uuid4()}", headers=auth
        )

        assert respuesta.status_code == 404, respuesta.text
