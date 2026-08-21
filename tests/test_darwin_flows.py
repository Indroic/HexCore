"""
Darwin Fase 5: los flujos completos, contra SQLite real.

Es el test que prueba que las cuatro capas se componen: dominio, persistencia, crypto y
aplicación. Un mock acá no probaría nada — el punto es que el sign-in de verdad hashee, guarde,
emita, y que el token que sale verifique contra la clave que se usó.

Los flujos adversariales que se fijan, cada uno por un motivo concreto:

- **Enumeración de usuarios**: el mismo error y el mismo tiempo para un mail inexistente que
  para una contraseña equivocada.
- **Orden de los chequeos**: "email no verificado" **después** de validar la contraseña. Al
  revés le confirma al atacante que el mail existe y que acertó la contraseña.
- **Reuso de refresh**: rotar dos veces con el mismo token revoca la familia entera.
- **Fijación de sesión**: el token cambia en el cambio de contraseña.
- **La sesión anterior muere al rotar**: sin eso, su access token sigue sirviendo hasta que
  venza.
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
    AccountLockedError,
    EmailAlreadyRegisteredError,
    EmailNotVerifiedError,
    FixedClock,
    IdentityConfig,
    InvalidCredentialsError,
    StaticKeyStore,
    TokenConfig,
    TokenExpiredError,
    TokenRevokedError,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    get_identity_container,
    reset_identity,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def contenedor(reloj):
    """
    Darwin configurado contra SQLite en memoria, con reloj y claves controlados.

    Se inyecta `StaticKeyStore` con una clave fija en vez de dejar que el contenedor genere una:
    el default genera una nueva en cada arranque, lo cual está bien para desarrollo pero haría
    que este test no pudiera verificar un token entre dos configuraciones.
    """
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))

    reset_identity()
    contenedor = configure_identity(
        IdentityConfig(
            secret_key=CLAVE,
            tokens=TokenConfig(issuer="https://api.test"),
            require_verified_email=True,
        ),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
    )
    yield contenedor

    reset_identity()
    asyncio.run(dispose_engine())


@pytest.fixture
def identidad(contenedor):
    return contenedor.identity_service()


@pytest.fixture
def sesiones(contenedor):
    return contenedor.session_service()


async def _usuario_verificado(identidad, email="ana@ejemplo.com", password="una frase larga"):
    usuario, codigo = await identidad.sign_up(email=email, password=password)
    await identidad.verify_email(email=email, code=codigo)
    return usuario


# ── El flujo completo ─────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_sign_up_verify_sign_in_refresh_sign_out(identidad, sesiones, contenedor):
    """El camino feliz completo, de punta a punta."""
    # Sign-up
    usuario, codigo = await identidad.sign_up(
        email="Ana@Ejemplo.COM", password="una frase larga y memorable", name="Ana"
    )
    assert usuario.email == "ana@ejemplo.com", "el mail tiene que normalizarse"
    assert usuario.email_verified is False
    assert len(codigo) == 6 and codigo.isdigit()

    # Verify
    verificado = await identidad.verify_email(email="ana@ejemplo.com", code=codigo)
    assert verificado.email_verified is True

    # Sign-in
    entrado, sesion, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga y memorable"
    )
    assert entrado.id == usuario.id
    assert par.access_token and par.refresh_token
    assert par.session_id == sesion.id
    assert sesion.actor_user_id == sesion.subject_user_id, "no es impersonación"

    # El access token verifica y reconstruye el contexto
    contexto = await sesiones.authenticate(par.access_token, transport="cookie")
    assert contexto.actor.user_id == usuario.id
    assert contexto.is_impersonating is False

    # Refresh
    nueva, par2 = await sesiones.refresh(par.refresh_token, transport="cookie")
    assert nueva.id != sesion.id
    assert nueva.family_id == sesion.family_id, "la rotación mantiene la familia"
    assert par2.access_token != par.access_token

    # Sign-out
    await sesiones.revoke(nueva.id)
    with pytest.raises(TokenRevokedError):
        await sesiones.authenticate(par2.access_token, transport="cookie")


# ── Enumeración de usuarios ───────────────────────────────────────────────────
@pytest.mark.anyio
async def test_un_mail_inexistente_y_una_clave_mala_dan_el_mismo_error(identidad):
    """
    **El invariante anti-enumeración.**

    Si difirieran, el atacante averigua qué mails están registrados sin adivinar ni una
    contraseña.
    """
    await _usuario_verificado(identidad)

    with pytest.raises(InvalidCredentialsError) as inexistente:
        await identidad.sign_in(email="nadie@ejemplo.com", password="cualquiera")

    with pytest.raises(InvalidCredentialsError) as clave_mala:
        await identidad.sign_in(email="ana@ejemplo.com", password="incorrecta")

    assert str(inexistente.value) == str(clave_mala.value)


@pytest.mark.anyio
async def test_un_mail_inexistente_igual_paga_el_costo_del_hash(identidad, monkeypatch):
    """
    Se verifica que `hash_dummy()` se llame, contando invocaciones.

    Medir tiempos sería escamoso en CI; contar la llamada es determinista y prueba lo mismo: sin
    ese hash, la rama "no encontré la fila" responde en microsegundos y la otra en decenas de
    milisegundos.
    """
    hasher = get_identity_container().hasher()
    llamadas: list[int] = []
    original = hasher.hash_dummy

    monkeypatch.setattr(
        hasher, "hash_dummy", lambda: (llamadas.append(1), original())[1]
    )

    with pytest.raises(InvalidCredentialsError):
        await identidad.sign_in(email="nadie@ejemplo.com", password="x")

    assert len(llamadas) == 1


@pytest.mark.anyio
async def test_el_email_sin_verificar_se_reporta_despues_de_validar_la_clave(identidad):
    """
    **El orden de los chequeos importa.**

    Con una contraseña **incorrecta** sobre una cuenta sin verificar, la respuesta tiene que ser
    `InvalidCredentialsError` y no `EmailNotVerifiedError`: lo segundo le confirmaría al
    atacante que el mail existe.

    Con la contraseña **correcta**, ahí sí se puede decir que falta verificar.
    """
    await identidad.sign_up(email="sin@verificar.com", password="una frase larga")

    with pytest.raises(InvalidCredentialsError):
        await identidad.sign_in(email="sin@verificar.com", password="incorrecta")

    with pytest.raises(EmailNotVerifiedError):
        await identidad.sign_in(email="sin@verificar.com", password="una frase larga")


@pytest.mark.anyio
async def test_una_cuenta_bloqueada_se_reporta_despues_de_validar_la_clave(
    identidad, contenedor
):
    """Mismo criterio: el bloqueo es información sobre una cuenta que existe."""
    usuario = await _usuario_verificado(identidad)
    usuarios = contenedor.users()
    await usuarios.update(
        usuario.model_copy(update={"locked_until": AHORA + timedelta(minutes=30)})
    )

    with pytest.raises(InvalidCredentialsError):
        await identidad.sign_in(email="ana@ejemplo.com", password="incorrecta")

    with pytest.raises(AccountLockedError):
        await identidad.sign_in(email="ana@ejemplo.com", password="una frase larga")


# ── Registro ──────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_no_se_puede_registrar_el_mismo_mail_dos_veces(identidad):
    await identidad.sign_up(email="ana@ejemplo.com", password="una frase larga")

    with pytest.raises(EmailAlreadyRegisteredError):
        await identidad.sign_up(email="ANA@ejemplo.com", password="otra frase larga")


@pytest.mark.anyio
async def test_la_politica_de_contrasenas_se_valida_antes_de_tocar_la_base(
    identidad, contenedor
):
    """
    Una contraseña inválida no puede dejar un usuario a medio crear.

    El orden importa: si se creara el usuario y después fallara la política, quedaría una fila
    sin credencial — y el mail ocupado, así que reintentar daría "ya existe".
    """
    with pytest.raises(ValueError, match="al menos"):
        await identidad.sign_up(email="corta@ejemplo.com", password="corta")

    assert await contenedor.users().get_by_email("corta@ejemplo.com") is None


@pytest.mark.anyio
async def test_un_codigo_de_verificacion_sirve_una_sola_vez(identidad):
    _, codigo = await identidad.sign_up(email="ana@ejemplo.com", password="una frase larga")
    await identidad.verify_email(email="ana@ejemplo.com", code=codigo)

    with pytest.raises(InvalidCredentialsError):
        await identidad.verify_email(email="ana@ejemplo.com", code=codigo)


@pytest.mark.anyio
async def test_un_codigo_equivocado_no_verifica(identidad):
    await identidad.sign_up(email="ana@ejemplo.com", password="una frase larga")

    with pytest.raises(InvalidCredentialsError):
        await identidad.verify_email(email="ana@ejemplo.com", code="000000")


@pytest.mark.anyio
async def test_reemitir_un_codigo_invalida_el_anterior(identidad):
    """
    Sin esto, cincuenta clicks en "reenviar" dejan cincuenta códigos válidos y el espacio a
    adivinar se multiplica por cincuenta.
    """
    _, primero = await identidad.sign_up(email="ana@ejemplo.com", password="una frase larga")
    segundo = await identidad.issue_verification(
        email="ana@ejemplo.com", purpose="email_verification"
    )

    with pytest.raises(InvalidCredentialsError):
        await identidad.verify_email(email="ana@ejemplo.com", code=primero)

    assert (await identidad.verify_email(email="ana@ejemplo.com", code=segundo)).email_verified


# ── Rotación y reuso ──────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_la_sesion_anterior_muere_al_rotar(identidad, sesiones, reloj):
    """
    Sin poner la anterior en la denylist, su access token sigue sirviendo hasta que venza — o
    sea hasta dos minutos después de haber rotado.
    """
    await _usuario_verificado(identidad)
    _, _, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga"
    )

    await sesiones.refresh(par.refresh_token, transport="cookie")

    with pytest.raises(TokenRevokedError):
        await sesiones.authenticate(par.access_token, transport="cookie")


@pytest.mark.anyio
async def test_reusar_un_refresh_revoca_la_familia_entera(identidad, sesiones, reloj):
    """
    **La detección de reuso.**

    Si el atacante y el usuario legítimo tienen los dos un token del linaje, revocar uno solo
    deja al otro adentro y no hay forma de saber cuál es cuál. Cae la familia.
    """
    await _usuario_verificado(identidad)
    _, _, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga"
    )

    nueva, par2 = await sesiones.refresh(par.refresh_token, transport="cookie")

    # Fuera de la ventana de gracia, reusar el primero es señal de robo.
    reloj.advance(seconds=30)
    with pytest.raises(TokenRevokedError, match="reuso"):
        await sesiones.refresh(par.refresh_token, transport="cookie")

    # Y la sesión legítima que salió de la rotación también cayó.
    with pytest.raises(TokenRevokedError):
        await sesiones.refresh(par2.refresh_token, transport="cookie")


@pytest.mark.anyio
async def test_la_ventana_de_gracia_no_dispara_la_deteccion(identidad, sesiones):
    """
    Dos pestañas, o un reintento tras un timeout de red, son el falso positivo más común de la
    detección de reuso — y el que hace que los equipos la terminen desactivando.

    Dentro de la gracia se rechaza el reintento pero **no** se revoca la familia.
    """
    await _usuario_verificado(identidad)
    _, _, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga"
    )
    _, par2 = await sesiones.refresh(par.refresh_token, transport="cookie")

    with pytest.raises(TokenRevokedError, match="instantes"):
        await sesiones.refresh(par.refresh_token, transport="cookie")

    # La sesión legítima sigue viva: la familia no cayó.
    tercera, _ = await sesiones.refresh(par2.refresh_token, transport="cookie")
    assert tercera is not None


@pytest.mark.anyio
async def test_rotar_no_extiende_el_techo_de_la_sesion(identidad, sesiones, reloj):
    """
    Si el techo se extendiera al rotar, rotar indefinidamente sería una sesión eterna y
    `session_ttl` no valdría para nada.
    """
    await _usuario_verificado(identidad)
    _, sesion, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga"
    )

    nueva, _ = await sesiones.refresh(par.refresh_token, transport="cookie")

    assert nueva.expires_at == sesion.expires_at


@pytest.mark.anyio
async def test_una_sesion_vencida_no_se_puede_rotar(identidad, sesiones, reloj):
    await _usuario_verificado(identidad)
    _, _, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga"
    )

    reloj.advance(days=91)  # más allá de session_ttl

    with pytest.raises((TokenExpiredError, TokenRevokedError)):
        await sesiones.refresh(par.refresh_token, transport="cookie")


@pytest.mark.anyio
async def test_una_sesion_revocada_no_se_puede_rotar(identidad, sesiones):
    await _usuario_verificado(identidad)
    _, sesion, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga"
    )
    await sesiones.revoke(sesion.id)

    with pytest.raises(TokenRevokedError):
        await sesiones.refresh(par.refresh_token, transport="cookie")


# ── Cambio de contraseña ──────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_cambiar_la_contrasena_revoca_todas_las_sesiones(
    identidad, sesiones, contenedor
):
    """
    **Fijación de sesión.**

    Es el flujo que la víctima ejecuta justamente para echar a un atacante que tiene una sesión
    abierta. Si no cortara las sesiones, no lo echaría.
    """
    usuario = await _usuario_verificado(identidad)
    _, _, par_a = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga", transport="cookie"
    )
    _, _, par_b = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga", transport="bearer"
    )

    await identidad.change_password(
        user_id=usuario.id,
        current_password="una frase larga",
        new_password="otra frase distinta y larga",
    )

    for par, transporte in ((par_a, "cookie"), (par_b, "bearer")):
        with pytest.raises(TokenRevokedError):
            await sesiones.authenticate(par.access_token, transport=transporte)

    # Y la contraseña nueva funciona.
    await identidad.sign_in(
        email="ana@ejemplo.com", password="otra frase distinta y larga"
    )


@pytest.mark.anyio
async def test_cambiar_la_contrasena_exige_la_actual(identidad):
    usuario = await _usuario_verificado(identidad)

    with pytest.raises(InvalidCredentialsError):
        await identidad.change_password(
            user_id=usuario.id,
            current_password="la equivocada",
            new_password="otra frase larga y valida",
        )


@pytest.mark.anyio
async def test_la_contrasena_nueva_tambien_pasa_por_la_politica(identidad):
    usuario = await _usuario_verificado(identidad)

    with pytest.raises(ValueError, match="al menos"):
        await identidad.change_password(
            user_id=usuario.id, current_password="una frase larga", new_password="corta"
        )


@pytest.mark.anyio
async def test_revoke_all_incrementa_la_generacion(identidad, sesiones, contenedor):
    """
    La capa 3 de la revocación: un solo UPDATE invalida todos los tokens del usuario, sin
    importar cuántas sesiones tenga.
    """
    usuario = await _usuario_verificado(identidad)
    await identidad.sign_in(email="ana@ejemplo.com", password="una frase larga")

    revocadas = await sesiones.revoke_all_for(usuario.id, reason="test")

    assert revocadas >= 1
    despues = await contenedor.users().get_by_id(usuario.id)
    assert despues is not None
    assert despues.token_generation > usuario.token_generation


@pytest.mark.anyio
async def test_un_token_de_generacion_vieja_no_rota(identidad, sesiones, contenedor):
    """
    El corte masivo tiene que valer también en el refresh: si el usuario cerró todo entre la
    emisión y el refresh, rotar no puede resucitar la sesión.
    """
    usuario = await _usuario_verificado(identidad)
    _, _, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga"
    )

    await contenedor.users().bump_token_generation(usuario.id)

    with pytest.raises(TokenRevokedError):
        await sesiones.refresh(par.refresh_token, transport="cookie")


# ── Transporte ────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_un_token_de_cookie_no_sirve_como_bearer(identidad, sesiones):
    """El `aud` ata el token a su transporte: replayear una cookie como Bearer esquivaría CSRF."""
    from hexcore.darwin import TokenAudienceMismatchError

    await _usuario_verificado(identidad)
    _, _, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga", transport="cookie"
    )

    with pytest.raises(TokenAudienceMismatchError):
        await sesiones.authenticate(par.access_token, transport="bearer")


@pytest.mark.anyio
async def test_el_access_token_vence(identidad, sesiones, reloj):
    await _usuario_verificado(identidad)
    _, _, par = await identidad.sign_in(
        email="ana@ejemplo.com", password="una frase larga"
    )

    reloj.advance(seconds=121 + 31)  # TTL + leeway

    with pytest.raises(TokenExpiredError):
        await sesiones.authenticate(par.access_token, transport="cookie")


# ── Impersonación ─────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_una_impersonacion_necesita_motivo_y_quien_la_autorizo(
    identidad, sesiones
):
    """El invariante del `AuthContext`, alcanzado desde el servicio."""
    operador = await _usuario_verificado(identidad, "op@ejemplo.com")
    cliente = await _usuario_verificado(identidad, "cli@ejemplo.com")

    with pytest.raises(ValueError, match="impersonation_reason"):
        await sesiones.create(actor=operador, subject=cliente)


@pytest.mark.anyio
async def test_una_impersonacion_auditable_se_puede_crear(identidad, sesiones):
    operador = await _usuario_verificado(identidad, "op@ejemplo.com")
    cliente = await _usuario_verificado(identidad, "cli@ejemplo.com")

    sesion, par = await sesiones.create(
        actor=operador,
        subject=cliente,
        impersonation_reason="ticket #4821",
        impersonation_granted_by=operador.id,
    )

    assert sesion.is_impersonated is True
    assert sesion.actor_user_id == operador.id
    assert sesion.subject_user_id == cliente.id

    contexto = await sesiones.authenticate(par.access_token, transport="cookie")
    assert contexto.is_impersonating is True
    assert contexto.actor.user_id == operador.id
    assert contexto.subject.user_id == cliente.id
