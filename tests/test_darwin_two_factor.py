"""
Darwin Fase 9: `two_factor`, contra SQLite y la app real.

Es el primer plugin que **intercepta** un flujo del núcleo, así que este archivo prueba dos
cosas a la vez: el TOTP en sí y que el punto de extensión del sign-in funcione.

Lo adversarial que se fija:

- **El primer paso no emite nada.** Con 2FA activo, un sign-in con la contraseña correcta
  devuelve 401 y **ningún** token ni cookie. Si emitiera una sesión "parcial", el 2FA sería
  decorativo desde el primer endpoint que se olvide de restringirla.
- **Replay del código**: el mismo código, criptográficamente válido, no sirve dos veces.
- **Replay concurrente**: ocho canjes simultáneos con el mismo desafío y el mismo código, gana
  exactamente uno.
- **El desafío es de un solo uso**, y se consume **antes** de verificar el código: si no, un
  desafío robado permitiría probar códigos indefinidamente.
- **El desafío de otro usuario no sirve** para completar el login de uno.
- **Techo de intentos** por fila, y el mismo error para "inválido", "ya usado" y "sin intentos".
- **El secreto no se guarda en claro** y el texto cifrado está autenticado: alterarlo un byte
  hace fallar el descifrado en vez de devolver otro secreto.
- **Inscribir no activa**: hasta que no se confirma, el sign-in sigue funcionando normal. Es lo
  que evita el bloqueo autoinfligido.
- **Desactivar exige código**, y no se puede hacer impersonando.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime

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
    PluginRegistry,
    StaticKeyStore,
    TokenConfig,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    reset_identity,
)
from hexcore.darwin.plugins.two_factor import (  # noqa: E402
    MAX_FAILED_ATTEMPTS,
    TwoFactorAlreadyConfirmedError,
    TwoFactorInvalidCodeError,
    TwoFactorNotEnrolledError,
    TwoFactorPlugin,
    TwoFactorRequiredError,
    get_two_factor_service,
)
from hexcore.darwin.plugins.two_factor.crypto import (  # noqa: E402
    SecretDecryptionError,
    TotpSecretCipher,
)
from hexcore.darwin.plugins.two_factor.models import (  # noqa: E402
    create_two_factor_tables,
)
from hexcore.darwin.plugins.two_factor.totp import (  # noqa: E402
    DEFAULT_STEP,
    current_step,
    generate_totp_secret,
    provisioning_uri,
    totp_code,
    verify_totp,
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


# ── El TOTP, sin nada más ─────────────────────────────────────────────────────
class TestTotp:
    """
    RFC 6238 sobre `hmac` de la stdlib. Sin base, sin contenedor: es aritmética.
    """

    def test_el_secreto_es_base32_de_160_bits(self):
        secreto = generate_totp_secret()

        # 20 bytes en base32 son exactamente 32 caracteres, o sea que no sobra relleno que
        # recortar. La propiedad que importa es que no lleve `=`: hay apps que lo rechazan y le
        # muestran al usuario un QR que no escanea, sin decir por qué.
        assert len(secreto) == 32
        assert "=" not in secreto
        assert len(base64.b32decode(secreto)) == 20

    def test_dos_secretos_no_se_repiten(self):
        assert generate_totp_secret() != generate_totp_secret()

    def test_el_vector_de_la_rfc_4226(self):
        """
        El secreto de prueba de la RFC 4226 (`12345678901234567890`) y sus primeros códigos.

        Es el único test que prueba que la implementación es **correcta** y no sólo
        autoconsistente: los diez valores están en el apéndice D de la especificación.
        """
        from hexcore.darwin.plugins.two_factor.totp import hotp_code

        secreto = base64.b32encode(b"12345678901234567890").decode()
        esperados = [
            "755224", "287082", "359152", "969429", "338314",
            "254676", "287922", "162583", "399871", "520489",
        ]

        assert [hotp_code(secreto, i) for i in range(10)] == esperados

    def test_el_codigo_es_estable_dentro_del_paso(self):
        secreto = generate_totp_secret()
        base = 1_786_017_600  # múltiplo de 30

        assert totp_code(secreto, base) == totp_code(secreto, base + 29)

    def test_el_codigo_cambia_al_paso_siguiente(self):
        secreto = generate_totp_secret()
        base = 1_786_017_600

        assert totp_code(secreto, base) != totp_code(secreto, base + DEFAULT_STEP)

    def test_verifica_y_devuelve_el_paso(self):
        secreto = generate_totp_secret()
        momento = 1_786_017_600.0

        paso = verify_totp(secreto, totp_code(secreto, momento), momento)

        assert paso == current_step(momento)

    def test_acepta_el_paso_anterior_y_el_siguiente(self):
        """
        La ventana de ±1 existe porque el reloj del teléfono deriva y el usuario tarda en
        tipear. Con ventana 0, un código copiado en el segundo 29 llega vencido.
        """
        secreto = generate_totp_secret()
        momento = 1_786_017_600.0

        anterior = totp_code(secreto, momento - DEFAULT_STEP)
        siguiente = totp_code(secreto, momento + DEFAULT_STEP)

        assert verify_totp(secreto, anterior, momento) is not None
        assert verify_totp(secreto, siguiente, momento) is not None

    def test_rechaza_dos_pasos_de_distancia(self):
        """La ventana no se agranda sola: cada paso extra duplica lo que un código robado sirve."""
        secreto = generate_totp_secret()
        momento = 1_786_017_600.0

        lejano = totp_code(secreto, momento - 2 * DEFAULT_STEP)

        assert verify_totp(secreto, lejano, momento) is None

    def test_after_step_rechaza_un_codigo_ya_usado(self):
        """
        La defensa de replay. El código sigue siendo criptográficamente válido: lo que lo
        invalida es que su paso ya se consumió.
        """
        secreto = generate_totp_secret()
        momento = 1_786_017_600.0
        codigo = totp_code(secreto, momento)
        paso = current_step(momento)

        assert verify_totp(secreto, codigo, momento) == paso
        assert verify_totp(secreto, codigo, momento, after_step=paso) is None

    @pytest.mark.parametrize(
        "basura", ["", "12345", "1234567", "abcdef", "12 34 56 78", "-12345"]
    )
    def test_rechaza_lo_que_no_es_un_codigo(self, basura):
        assert verify_totp(generate_totp_secret(), basura, 1_786_017_600.0) is None

    def test_tolera_espacios_y_guiones(self):
        """Las apps muestran el código en grupos de tres; el usuario copia el separador."""
        secreto = generate_totp_secret()
        momento = 1_786_017_600.0
        codigo = totp_code(secreto, momento)

        con_espacio = f"{codigo[:3]} {codigo[3:]}"
        con_guion = f"{codigo[:3]}-{codigo[3:]}"

        assert verify_totp(secreto, con_espacio, momento) is not None
        assert verify_totp(secreto, con_guion, momento) is not None

    def test_el_secreto_de_otro_no_valida(self):
        momento = 1_786_017_600.0
        codigo = totp_code(generate_totp_secret(), momento)

        assert verify_totp(generate_totp_secret(), codigo, momento) is None

    def test_la_uri_lleva_el_issuer_dos_veces(self):
        """
        En la etiqueta y como parámetro. No es redundancia: la etiqueta la muestran las apps
        viejas y el parámetro lo leen las nuevas — con sólo el parámetro, un usuario con tres
        cuentas ve tres entradas idénticas.
        """
        uri = provisioning_uri("JBSWY3DPEHPK3PXP", account="ana@ejemplo.com", issuer="Mi App")

        assert uri.startswith("otpauth://totp/Mi%20App%3Aana%40ejemplo.com?")
        assert "issuer=Mi+App" in uri
        assert "algorithm=SHA1" in uri
        assert "digits=6" in uri and "period=30" in uri


# ── El cifrado del secreto ────────────────────────────────────────────────────
class TestCifrado:
    def test_ida_y_vuelta(self):
        cifrador = TotpSecretCipher(CLAVE)
        secreto = generate_totp_secret()

        assert cifrador.decrypt(cifrador.encrypt(secreto)) == secreto

    def test_el_texto_cifrado_no_contiene_el_secreto(self):
        cifrador = TotpSecretCipher(CLAVE)
        secreto = generate_totp_secret()

        guardado = cifrador.encrypt(secreto)

        assert secreto not in guardado

    def test_dos_cifrados_del_mismo_secreto_difieren(self):
        """Nonce nuevo por llamada: si no, dos usuarios con el mismo secreto serían visibles."""
        cifrador = TotpSecretCipher(CLAVE)
        secreto = generate_totp_secret()

        assert cifrador.encrypt(secreto) != cifrador.encrypt(secreto)

    def test_otra_clave_no_descifra(self):
        """La propiedad entera: un dump sin la clave de la aplicación no sirve para nada."""
        guardado = TotpSecretCipher(CLAVE).encrypt(generate_totp_secret())

        with pytest.raises(SecretDecryptionError):
            TotpSecretCipher("otra" * 12).decrypt(guardado)

    def test_alterar_un_byte_hace_fallar_el_descifrado(self):
        """
        AEAD: el texto cifrado está **autenticado**. Sin eso, alguien con escritura en la base
        podría sustituir el secreto de un usuario por uno que él conoce.
        """
        cifrador = TotpSecretCipher(CLAVE)
        guardado = cifrador.encrypt(generate_totp_secret())

        partes = guardado.split(".")
        partes[3] = "A" + partes[3][1:] if partes[3][0] != "A" else "B" + partes[3][1:]
        alterado = ".".join(partes)

        with pytest.raises(SecretDecryptionError):
            cifrador.decrypt(alterado)

    def test_la_clave_de_cifrado_no_es_secret_key_directo(self):
        """
        Se deriva con una etiqueta propia. Reusar el mismo material para cifrar secretos TOTP,
        firmar sobres y derivar valores anti-CSRF hace que romper uno rompa los tres.
        """
        from hexcore.darwin.infrastructure.hashing import derive_csrf_token

        cifrador = TotpSecretCipher(CLAVE)
        guardado = cifrador.encrypt("JBSWY3DPEHPK3PXP")

        # El valor anti-CSRF derivado de la misma clave no aparece en el texto cifrado.
        assert derive_csrf_token("x", CLAVE) not in guardado
        # Y una clave distinta por un caracter da un resultado no intercambiable.
        with pytest.raises(SecretDecryptionError):
            TotpSecretCipher(CLAVE[:-1] + "z").decrypt(guardado)


# ── Los flujos, contra SQLite ─────────────────────────────────────────────────
@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def plugin() -> TwoFactorPlugin:
    return TwoFactorPlugin(issuer="Test App")


@pytest.fixture
def contenedor(reloj, plugin):
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))
    asyncio.run(create_two_factor_tables(motor))

    from hexcore.config import LazyConfig
    from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

    # El `rate_limit` de los routers usa el `MemoryCache` global del proceso: sin resetearlo,
    # el contador se acumula entre tests y del sexto en adelante todo da 429.
    LazyConfig.get_config().cache_backend = MemoryCache()

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
    yield contenedor

    reset_identity()
    plugin.reset()
    asyncio.run(dispose_engine())


@pytest.fixture
def servicio(contenedor):
    return get_two_factor_service()


async def _alta(contenedor, email: str = MAIL):
    usuario, _ = await contenedor.identity_service().sign_up(email=email, password=PASS)
    return await contenedor.users().update(
        usuario.model_copy(update={"email_verified": True})
    )


async def _con_2fa(contenedor, servicio, reloj, email: str = MAIL):
    """Alta + inscripción + confirmación. Devuelve `(usuario, secreto)`."""
    usuario = await _alta(contenedor, email)
    inscripcion = await servicio.enroll(user_id=usuario.id, account=email)
    await servicio.confirm(
        user_id=usuario.id,
        code=totp_code(inscripcion.secret, reloj.now().timestamp()),
    )
    return usuario, inscripcion.secret


class TestInscripcion:
    @pytest.mark.anyio
    async def test_inscribir_no_activa(self, contenedor, servicio, reloj):
        """
        La decisión que evita el bloqueo autoinfligido: si inscribir activara el factor, el
        usuario que guardó mal el QR queda afuera y sólo lo saca una intervención humana.
        """
        usuario = await _alta(contenedor)

        inscripcion = await servicio.enroll(user_id=usuario.id, account=MAIL)

        assert inscripcion.confirmed is False
        assert await servicio.is_required_for(usuario.id) is False
        assert await servicio.describe(usuario.id) == (True, False)

        # Y el sign-in sigue funcionando normal.
        _, _, par = await contenedor.identity_service().sign_in(
            email=MAIL, password=PASS
        )
        assert par.access_token

    @pytest.mark.anyio
    async def test_confirmar_activa(self, contenedor, servicio, reloj):
        usuario = await _alta(contenedor)
        inscripcion = await servicio.enroll(user_id=usuario.id, account=MAIL)

        factor = await servicio.confirm(
            user_id=usuario.id,
            code=totp_code(inscripcion.secret, reloj.now().timestamp()),
        )

        assert factor.is_confirmed
        assert await servicio.is_required_for(usuario.id) is True
        assert await servicio.describe(usuario.id) == (True, True)

    @pytest.mark.anyio
    async def test_confirmar_con_un_codigo_malo_no_activa(self, contenedor, servicio):
        usuario = await _alta(contenedor)
        await servicio.enroll(user_id=usuario.id, account=MAIL)

        with pytest.raises(TwoFactorInvalidCodeError):
            await servicio.confirm(user_id=usuario.id, code="000000")

        assert await servicio.is_required_for(usuario.id) is False

    @pytest.mark.anyio
    async def test_confirmar_sin_inscribir_falla(self, contenedor, servicio):
        usuario = await _alta(contenedor)

        with pytest.raises(TwoFactorNotEnrolledError):
            await servicio.confirm(user_id=usuario.id, code="000000")

    @pytest.mark.anyio
    async def test_confirmar_dos_veces_falla(self, contenedor, servicio, reloj):
        usuario, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)

        with pytest.raises(TwoFactorAlreadyConfirmedError):
            await servicio.confirm(
                user_id=usuario.id,
                code=totp_code(secreto, reloj.now().timestamp()),
            )

    @pytest.mark.anyio
    async def test_reinscribir_con_uno_activo_falla(self, contenedor, servicio, reloj):
        """Rotaría el secreto en silencio y el usuario quedaría con el QR viejo."""
        usuario, _ = await _con_2fa(contenedor, servicio, reloj)

        with pytest.raises(TwoFactorAlreadyConfirmedError):
            await servicio.enroll(user_id=usuario.id, account=MAIL)

    @pytest.mark.anyio
    async def test_reinscribir_sin_confirmar_reemplaza(self, contenedor, servicio):
        """Un usuario que perdió el QR antes de confirmar tiene que poder pedir otro."""
        usuario = await _alta(contenedor)
        primera = await servicio.enroll(user_id=usuario.id, account=MAIL)

        segunda = await servicio.enroll(user_id=usuario.id, account=MAIL)

        assert segunda.secret != primera.secret
        assert await servicio.describe(usuario.id) == (True, False)

    @pytest.mark.anyio
    async def test_el_secreto_se_guarda_cifrado(self, contenedor, servicio):
        """La fila no contiene el secreto: un dump no genera códigos."""
        usuario = await _alta(contenedor)
        inscripcion = await servicio.enroll(user_id=usuario.id, account=MAIL)

        from sqlalchemy import select

        from hexcore.darwin.plugins.two_factor.models import TwoFactorModel
        from hexcore.infrastructure.uow.scopes import session_scope

        async with session_scope() as sesion:
            resultado = await sesion.execute(select(TwoFactorModel))
            fila = resultado.scalar_one()

        assert inscripcion.secret not in fila.secret_encrypted
        assert TotpSecretCipher(CLAVE).decrypt(fila.secret_encrypted) == inscripcion.secret

    @pytest.mark.anyio
    async def test_una_sola_fila_por_usuario(self, contenedor, servicio):
        """El `UNIQUE`: dos filas dejarían un secreto abandonado sirviendo para entrar."""
        usuario = await _alta(contenedor)
        await servicio.enroll(user_id=usuario.id, account=MAIL)
        await servicio.enroll(user_id=usuario.id, account=MAIL)

        from sqlalchemy import func, select

        from hexcore.darwin.plugins.two_factor.models import TwoFactorModel
        from hexcore.infrastructure.uow.scopes import session_scope

        async with session_scope() as sesion:
            resultado = await sesion.execute(
                select(func.count()).select_from(TwoFactorModel)
            )
            assert int(resultado.scalar_one()) == 1


class TestSignInEnDosPasos:
    @pytest.mark.anyio
    async def test_el_primer_paso_no_emite_nada(self, contenedor, servicio, reloj):
        """
        **La propiedad central del plugin.** Con 2FA activo, la contraseña correcta no produce
        ninguna sesión ni ningún token: sólo un desafío.
        """
        await _con_2fa(contenedor, servicio, reloj)

        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)

        assert excinfo.value.challenge, "el desafío es lo único que sale del primer paso"

        # Y no quedó ninguna sesión en la base.
        from sqlalchemy import func, select

        from hexcore.darwin.infrastructure.models import SessionModel
        from hexcore.infrastructure.uow.scopes import session_scope

        async with session_scope() as sesion:
            resultado = await sesion.execute(
                select(func.count()).select_from(SessionModel)
            )
            assert int(resultado.scalar_one()) == 0

    @pytest.mark.anyio
    async def test_la_contrasena_mala_sigue_dando_el_error_de_credenciales(
        self, contenedor, servicio, reloj
    ):
        """
        El hook corre **después** de validar la contraseña. Al revés, pedir el segundo factor
        antes le confirmaría al atacante que el mail existe.
        """
        await _con_2fa(contenedor, servicio, reloj)

        with pytest.raises(InvalidCredentialsError):
            await contenedor.identity_service().sign_in(email=MAIL, password="otra cosa")

    @pytest.mark.anyio
    async def test_el_segundo_paso_abre_la_sesion(self, contenedor, servicio, reloj):
        usuario, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)

        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        desafio = excinfo.value.challenge or ""

        entrado, sesion, par = await servicio.complete_sign_in(
            challenge=desafio,
            code=totp_code(secreto, reloj.now().timestamp()),
        )

        assert entrado.id == usuario.id
        assert sesion.actor_user_id == usuario.id
        assert par.access_token and par.refresh_token

    @pytest.mark.anyio
    async def test_el_desafio_es_de_un_solo_uso(self, contenedor, servicio, reloj):
        _, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)

        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        desafio = excinfo.value.challenge or ""

        await servicio.complete_sign_in(
            challenge=desafio, code=totp_code(secreto, reloj.now().timestamp())
        )

        reloj.advance(seconds=DEFAULT_STEP)
        with pytest.raises(InvalidCredentialsError):
            await servicio.complete_sign_in(
                challenge=desafio, code=totp_code(secreto, reloj.now().timestamp())
            )

    @pytest.mark.anyio
    async def test_el_desafio_se_consume_aunque_el_codigo_sea_malo(
        self, contenedor, servicio, reloj
    ):
        """
        ⚠️ El desafío se consume **antes** de verificar el código. Si se consumiera después,
        quien tenga el desafío podría probar códigos indefinidamente sobre el mismo; así, cada
        intento cuesta un desafío nuevo — o sea la contraseña.
        """
        _, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)

        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        desafio = excinfo.value.challenge or ""

        with pytest.raises(TwoFactorInvalidCodeError):
            await servicio.complete_sign_in(challenge=desafio, code="000000")

        # El desafío ya no sirve, ni con el código correcto.
        with pytest.raises(InvalidCredentialsError):
            await servicio.complete_sign_in(
                challenge=desafio, code=totp_code(secreto, reloj.now().timestamp())
            )

    @pytest.mark.anyio
    async def test_pedir_un_desafio_nuevo_invalida_el_anterior(
        self, contenedor, servicio, reloj
    ):
        """Cinco intentos de login no pueden dejar cinco vales para completar uno."""
        _, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)

        with pytest.raises(TwoFactorRequiredError) as primero:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        with pytest.raises(TwoFactorRequiredError) as segundo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)

        with pytest.raises(InvalidCredentialsError):
            await servicio.complete_sign_in(
                challenge=primero.value.challenge or "",
                code=totp_code(secreto, reloj.now().timestamp()),
            )

        _, _, par = await servicio.complete_sign_in(
            challenge=segundo.value.challenge or "",
            code=totp_code(secreto, reloj.now().timestamp()),
        )
        assert par.access_token

    @pytest.mark.anyio
    async def test_un_desafio_vencido_no_sirve(self, contenedor, servicio, reloj):
        _, secreto = await _con_2fa(contenedor, servicio, reloj)

        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        desafio = excinfo.value.challenge or ""

        reloj.advance(minutes=6)  # el TTL por default son 5

        with pytest.raises(InvalidCredentialsError):
            await servicio.complete_sign_in(
                challenge=desafio, code=totp_code(secreto, reloj.now().timestamp())
            )

    @pytest.mark.anyio
    async def test_el_desafio_de_otro_no_sirve(self, contenedor, servicio, reloj):
        """
        El canje filtra por `identifier`: el desafío de Ana con el código de Ana no completa el
        login de Beto, ni al revés.
        """
        _, secreto_ana = await _con_2fa(contenedor, servicio, reloj)
        _, secreto_beto = await _con_2fa(
            contenedor, servicio, reloj, email="beto@ejemplo.com"
        )
        reloj.advance(seconds=DEFAULT_STEP)

        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        desafio_de_ana = excinfo.value.challenge or ""

        # El código de Beto contra el desafío de Ana: el desafío se consume y el código falla.
        with pytest.raises(TwoFactorInvalidCodeError):
            await servicio.complete_sign_in(
                challenge=desafio_de_ana,
                code=totp_code(secreto_beto, reloj.now().timestamp()),
            )
        assert secreto_ana != secreto_beto

    @pytest.mark.parametrize(
        "malformado",
        ["", "sin-punto", ".", "no-es-uuid.token", "abc.", ".token"],
    )
    @pytest.mark.anyio
    async def test_un_desafio_malformado_da_401_y_no_500(
        self, contenedor, servicio, malformado
    ):
        """
        El valor lo manda el cliente. Un `ValueError` de `UUID` acá sería un 500 que le dice a
        quien prueba formatos que encontró un camino no manejado.
        """
        with pytest.raises(InvalidCredentialsError):
            await servicio.complete_sign_in(challenge=malformado, code="000000")


class TestReplay:
    @pytest.mark.anyio
    async def test_el_mismo_codigo_no_sirve_dos_veces(self, contenedor, servicio, reloj):
        """
        Un código vale hasta 90 segundos con la ventana por default: quien lo lee por encima
        del hombro, o lo saca de un formulario de phishing, lo puede volver a usar. El paso
        consumido es lo que convierte "es válido" en "es válido y no se usó".
        """
        _, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)
        codigo = totp_code(secreto, reloj.now().timestamp())

        with pytest.raises(TwoFactorRequiredError) as primero:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        await servicio.complete_sign_in(
            challenge=primero.value.challenge or "", code=codigo
        )

        with pytest.raises(TwoFactorRequiredError) as segundo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        with pytest.raises(TwoFactorInvalidCodeError):
            await servicio.complete_sign_in(
                challenge=segundo.value.challenge or "", code=codigo
            )

    @pytest.mark.anyio
    async def test_ocho_canjes_concurrentes_dejan_pasar_uno(
        self, contenedor, servicio, reloj
    ):
        """
        Es la razón por la que `consume` del desafío y `consume_step` son sentencias únicas.
        Con leer-y-después-escribir, ocho peticiones con el mismo desafío y el mismo código
        pasarían todas — que es exactamente cuando el replay importa.
        """
        _, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)
        codigo = totp_code(secreto, reloj.now().timestamp())

        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        desafio = excinfo.value.challenge or ""

        resultados = await asyncio.gather(
            *(
                servicio.complete_sign_in(challenge=desafio, code=codigo)
                for _ in range(8)
            ),
            return_exceptions=True,
        )

        ganaron = [r for r in resultados if not isinstance(r, BaseException)]
        perdieron = [
            r
            for r in resultados
            if isinstance(r, (InvalidCredentialsError, TwoFactorInvalidCodeError))
        ]

        assert len(ganaron) == 1, f"ganaron {len(ganaron)}"
        assert len(perdieron) == 7, "y las otras siete fallaron por el motivo correcto"

    @pytest.mark.anyio
    async def test_el_paso_siguiente_si_sirve(self, contenedor, servicio, reloj):
        """
        La defensa de replay no rompe el uso normal: 30 segundos después, el código nuevo entra.
        """
        _, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)

        with pytest.raises(TwoFactorRequiredError) as primero:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        await servicio.complete_sign_in(
            challenge=primero.value.challenge or "",
            code=totp_code(secreto, reloj.now().timestamp()),
        )

        reloj.advance(seconds=DEFAULT_STEP)
        with pytest.raises(TwoFactorRequiredError) as segundo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)

        _, _, par = await servicio.complete_sign_in(
            challenge=segundo.value.challenge or "",
            code=totp_code(secreto, reloj.now().timestamp()),
        )
        assert par.access_token


class TestTechoDeIntentos:
    @pytest.mark.anyio
    async def test_los_intentos_fallidos_bloquean(self, contenedor, servicio, reloj):
        """
        Un OTP de 6 dígitos con ventana de ±1 deja 3 códigos válidos de 10⁶: sin techo, el
        ataque cierra en horas.
        """
        usuario, secreto = await _con_2fa(contenedor, servicio, reloj)

        for _ in range(MAX_FAILED_ATTEMPTS):
            await servicio.record_failed_attempt(usuario.id)

        reloj.advance(seconds=DEFAULT_STEP)
        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)

        # Ni el código correcto entra: el techo se chequea antes de calcular nada.
        with pytest.raises(TwoFactorInvalidCodeError):
            await servicio.complete_sign_in(
                challenge=excinfo.value.challenge or "",
                code=totp_code(secreto, reloj.now().timestamp()),
            )

    @pytest.mark.anyio
    async def test_un_codigo_valido_resetea_los_intentos(
        self, contenedor, servicio, reloj
    ):
        """Un usuario con el reloj corrido falla dos veces y no puede quedar marcado por eso."""
        usuario, secreto = await _con_2fa(contenedor, servicio, reloj)
        await servicio.record_failed_attempt(usuario.id)
        await servicio.record_failed_attempt(usuario.id)

        reloj.advance(seconds=DEFAULT_STEP)
        with pytest.raises(TwoFactorRequiredError) as excinfo:
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
        await servicio.complete_sign_in(
            challenge=excinfo.value.challenge or "",
            code=totp_code(secreto, reloj.now().timestamp()),
        )

        factor = await servicio._repo.get_for_user(usuario.id)  # noqa: SLF001
        assert factor is not None and factor.failed_attempts == 0


class TestDesactivar:
    @pytest.mark.anyio
    async def test_desactivar_exige_un_codigo_valido(self, contenedor, servicio, reloj):
        """
        Sin esto, quien roba una sesión con el 2FA ya pasado apaga el segundo factor y se queda
        con la cuenta. Es la operación que más protección necesita, no menos.
        """
        usuario, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)

        with pytest.raises(TwoFactorInvalidCodeError):
            await servicio.disable(user_id=usuario.id, code="000000")
        assert await servicio.is_required_for(usuario.id) is True

        await servicio.disable(
            user_id=usuario.id, code=totp_code(secreto, reloj.now().timestamp())
        )
        assert await servicio.is_required_for(usuario.id) is False
        assert await servicio.describe(usuario.id) == (False, False)

    @pytest.mark.anyio
    async def test_desactivado_el_sign_in_vuelve_a_un_paso(
        self, contenedor, servicio, reloj
    ):
        usuario, secreto = await _con_2fa(contenedor, servicio, reloj)
        reloj.advance(seconds=DEFAULT_STEP)
        await servicio.disable(
            user_id=usuario.id, code=totp_code(secreto, reloj.now().timestamp())
        )

        _, _, par = await contenedor.identity_service().sign_in(
            email=MAIL, password=PASS
        )

        assert par.access_token

    @pytest.mark.anyio
    async def test_desactivar_sin_inscribir_falla(self, contenedor, servicio):
        usuario = await _alta(contenedor)

        with pytest.raises(TwoFactorNotEnrolledError):
            await servicio.disable(user_id=usuario.id, code="000000")


# ── El plugin como plugin ─────────────────────────────────────────────────────
class TestPlugin:
    def test_aporta_su_mixin_y_no_una_clase_mapeada(self, plugin):
        """
        Mixin y no modelo: el consumidor lo compone en su paquete `models/`, que es lo que hace
        que `--autogenerate` vea la tabla en vez de emitirle un `op.drop_table`.
        """
        from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

        mixins = plugin.tables()

        assert list(mixins) == ["TwoFactorMixin"]
        assert not issubclass(mixins["TwoFactorMixin"], Base)

    def test_aporta_su_mapa_de_excepciones(self, plugin):
        mapa = plugin.exception_status_map()

        assert mapa[TwoFactorRequiredError] == 401
        assert mapa[TwoFactorNotEnrolledError] == 409

    def test_no_mapea_la_excepcion_base(self, plugin):
        """
        Por lo mismo que el núcleo no mapea `IdentityError`: `_specificity` ordena por
        profundidad de MRO, así que mapearla haría que una falla nueva se tragara con ese status
        en vez de aparecer como un 500 en los tests.
        """
        from hexcore.darwin.plugins.two_factor import TwoFactorError

        assert TwoFactorError not in plugin.exception_status_map()

    def test_se_engancha_al_punto_de_extension_del_sign_in(self, plugin):
        from hexcore.darwin.application.services import SIGN_IN_AUTHENTICATED

        acciones = [(b.action, b.phase) for b in plugin.hooks()]

        assert acciones == [(SIGN_IN_AUTHENTICATED, "before")]

    def test_registra_sus_cuatro_handlers(self, plugin):
        from hexcore.application.cqrs.registry import HandlerRegistry
        from hexcore.darwin.plugins.two_factor.commands import (
            CompleteTwoFactorSignIn,
            ConfirmTwoFactor,
            DisableTwoFactor,
            EnrollTwoFactor,
        )

        registro = HandlerRegistry()
        plugin.register_handlers(registro)

        for comando in (
            EnrollTwoFactor,
            ConfirmTwoFactor,
            DisableTwoFactor,
            CompleteTwoFactorSignIn,
        ):
            assert registro.resolve_command_handler(comando) is not None

    def test_los_comandos_declaran_su_accion(self):
        from hexcore.darwin import action_of
        from hexcore.darwin.plugins.two_factor.commands import (
            CompleteTwoFactorSignIn,
            EnrollTwoFactor,
        )

        assert action_of(EnrollTwoFactor) == "two_factor.enroll"
        assert action_of(CompleteTwoFactorSignIn) == "two_factor.complete_sign_in"

    def test_el_servicio_sin_registrar_falla_con_remediacion(self, reloj):
        from hexcore.darwin import reset_identity as limpiar

        limpiar()
        configure_identity(
            IdentityConfig(secret_key=CLAVE),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        )
        try:
            with pytest.raises(RuntimeError) as excinfo:
                get_two_factor_service()

            mensaje = str(excinfo.value)
            assert "no está registrado" in mensaje
            assert "TwoFactorPlugin" in mensaje
        finally:
            limpiar()

    def test_el_registro_lo_valida(self, plugin):
        registro = PluginRegistry([plugin])
        registro.validate()

        assert registro.names == ("two_factor",)


# ── El borde HTTP ─────────────────────────────────────────────────────────────
@pytest.fixture
def cliente(contenedor, plugin):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hexcore.darwin import build_identity_router
    from hexcore.fastapi import AppFeatures, create_app

    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True, health=False),
        routers=[build_identity_router(), *plugin.routers()],
    )
    with TestClient(app) as cliente:
        yield cliente


class TestHttp:
    @pytest.mark.anyio
    async def test_el_sign_in_con_2fa_da_401_sin_cookies(
        self, contenedor, servicio, reloj, cliente
    ):
        """
        401 y no 403: la autenticación **no terminó**. Un 403 diría que está autenticado y no
        autorizado, que es lo contrario.
        """
        await _con_2fa(contenedor, servicio, reloj)

        respuesta = cliente.post(
            "/auth/sign-in", json={"email": MAIL, "password": PASS}
        )

        assert respuesta.status_code == 401
        assert not respuesta.headers.get_list("set-cookie"), "no se emite ninguna cookie"

    @pytest.mark.anyio
    async def test_el_flujo_completo_por_http(self, contenedor, servicio, reloj, cliente):
        usuario = await _alta(contenedor)

        # 1. Login normal, sin 2FA todavía.
        entrada = cliente.post(
            "/auth/sign-in",
            json={"email": MAIL, "password": PASS},
            headers={"X-Darwin-Transport": "bearer"},
        )
        assert entrada.status_code == 200
        token = entrada.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        # 2. Estado inicial.
        estado = cliente.get("/auth/2fa", headers=auth)
        assert estado.json() == {"enrolled": False, "confirmed": False}

        # 3. Inscribir.
        inscripcion = cliente.post("/auth/2fa/enroll", headers=auth)
        assert inscripcion.status_code == 201
        secreto = inscripcion.json()["secret"]
        assert inscripcion.json()["uri"].startswith("otpauth://totp/Test%20App")
        assert inscripcion.json()["confirmed"] is False

        # 4. Confirmar.
        reloj.advance(seconds=DEFAULT_STEP)
        confirmacion = cliente.post(
            "/auth/2fa/confirm",
            json={"code": totp_code(secreto, reloj.now().timestamp())},
            headers=auth,
        )
        assert confirmacion.status_code == 200
        assert cliente.get("/auth/2fa", headers=auth).json() == {
            "enrolled": True,
            "confirmed": True,
        }

        # 5. Ahora el login pide el segundo factor.
        reloj.advance(seconds=DEFAULT_STEP)
        parcial = cliente.post(
            "/auth/sign-in",
            json={"email": MAIL, "password": PASS},
            headers={"X-Darwin-Transport": "bearer"},
        )
        assert parcial.status_code == 401

        # El desafío no viaja en el cuerpo del 401 del handler genérico, así que el segundo
        # paso se arma con el que emite el servicio — que es lo que haría una app que envuelve
        # la ruta para incluirlo.
        desafio = await servicio.issue_challenge(user=usuario)

        # 6. El canje.
        canje = cliente.post(
            "/auth/2fa/challenge",
            json={
                "challenge": desafio,
                "code": totp_code(secreto, reloj.now().timestamp()),
            },
            headers={"X-Darwin-Transport": "bearer"},
        )
        assert canje.status_code == 200
        assert canje.json()["access_token"]

    @pytest.mark.anyio
    async def test_el_canje_con_un_codigo_malo_da_401(
        self, contenedor, servicio, reloj, cliente
    ):
        usuario, _ = await _con_2fa(contenedor, servicio, reloj)
        desafio = await servicio.issue_challenge(user=usuario)

        respuesta = cliente.post(
            "/auth/2fa/challenge",
            json={"challenge": desafio, "code": "000000"},
            headers={"X-Darwin-Transport": "bearer"},
        )

        assert respuesta.status_code == 401
        assert "WWW-Authenticate" in respuesta.headers

    @pytest.mark.anyio
    async def test_inscribir_sin_sesion_da_401(self, contenedor, cliente):
        respuesta = cliente.post("/auth/2fa/enroll")

        assert respuesta.status_code == 401

    @pytest.mark.anyio
    async def test_confirmar_sin_inscribir_da_409(self, contenedor, cliente):
        """El status del plugin, que llegó al borde por `exception_status_map()`."""
        await _alta(contenedor)
        entrada = cliente.post(
            "/auth/sign-in",
            json={"email": MAIL, "password": PASS},
            headers={"X-Darwin-Transport": "bearer"},
        )
        auth = {"Authorization": f"Bearer {entrada.json()['access_token']}"}

        respuesta = cliente.post(
            "/auth/2fa/confirm", json={"code": "000000"}, headers=auth
        )

        assert respuesta.status_code == 409, respuesta.text

    @pytest.mark.anyio
    async def test_el_rate_limit_frena_el_canje_numero_once(
        self, contenedor, servicio, reloj, cliente
    ):
        """
        Es la ruta donde se prueban códigos de 6 dígitos. El techo por fila sólo protege a un
        usuario ya inscripto; el límite por IP es lo que corta a quien rota entre cuentas.
        """
        usuario, _ = await _con_2fa(contenedor, servicio, reloj)
        desafio = await servicio.issue_challenge(user=usuario)

        codigos = [
            cliente.post(
                "/auth/2fa/challenge",
                json={"challenge": desafio, "code": "000000"},
            ).status_code
            for _ in range(11)
        ]

        assert codigos[:10] == [401] * 10
        assert codigos[10] == 429


# ── El punto de extensión, en general ─────────────────────────────────────────
@pytest.mark.anyio
async def test_un_hook_del_sign_in_puede_abortar_sin_ser_two_factor(
    contenedor, reloj
):
    """
    El punto de extensión no es de `two_factor`: cualquier plugin puede exigir algo ahí. Se
    prueba con un bloqueo por país, que es el otro caso obvio.
    """
    from hexcore.darwin import DarwinPlugin, HookBinding
    from hexcore.darwin.application.services import SIGN_IN_AUTHENTICATED
    from hexcore.darwin.domain.exceptions import AuthorizationError

    class BloqueoPorPais(DarwinPlugin):
        name = "bloqueo_por_pais"

        def hooks(self):
            async def bloquear(usuario):
                raise AuthorizationError("Ese país está bloqueado.")

            return [
                HookBinding(
                    action=SIGN_IN_AUTHENTICATED, phase="before", handler=bloquear
                )
            ]

    from hexcore.darwin import configure_identity as configurar, reset_identity as limpiar

    limpiar()
    contenedor = configurar(
        IdentityConfig(secret_key=CLAVE, require_verified_email=False),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        plugins=PluginRegistry([BloqueoPorPais()]),
    )
    try:
        await _alta(contenedor)

        with pytest.raises(AuthorizationError, match="bloqueado"):
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
    finally:
        limpiar()


@pytest.mark.anyio
async def test_un_hook_del_sign_in_que_explota_no_deja_entrar(contenedor, reloj):
    """
    Falla cerrando. Tragar la excepción dejaría que un hook de autorización con un bug se lea
    como uno que autorizó — el peor modo de falla posible.
    """
    from hexcore.darwin import DarwinPlugin, HookBinding
    from hexcore.darwin.application.services import SIGN_IN_AUTHENTICATED
    from hexcore.darwin import configure_identity as configurar, reset_identity as limpiar

    class ConBug(DarwinPlugin):
        name = "con_bug"

        def hooks(self):
            async def explotar(usuario):
                raise ValueError("me olvidé de un caso")

            return [
                HookBinding(
                    action=SIGN_IN_AUTHENTICATED, phase="before", handler=explotar
                )
            ]

    limpiar()
    contenedor = configurar(
        IdentityConfig(secret_key=CLAVE, require_verified_email=False),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        plugins=PluginRegistry([ConBug()]),
    )
    try:
        await _alta(contenedor)

        with pytest.raises(RuntimeError, match="con_bug"):
            await contenedor.identity_service().sign_in(email=MAIL, password=PASS)
    finally:
        limpiar()
