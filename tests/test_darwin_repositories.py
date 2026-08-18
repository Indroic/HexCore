"""
Darwin Fase 3: los adaptadores SQLAlchemy contra SQLite real.

Lo que más importa acá son las **operaciones atómicas**. `consume_for_rotation` y `consume`
tienen que resolverse con una sola sentencia ``UPDATE ... WHERE ... RETURNING``: con
leer-y-después-escribir, dos peticiones concurrentes con el mismo token pasan las dos, y
entonces la detección de reuso —el único mecanismo que detecta un refresh robado— no dispara
nunca, y un magic link "de un solo uso" sirve dos veces.

Se usan los fixtures que ya trae el framework (`hexcore.testing.fixtures`) en vez de armar un
engine a mano.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    CREDENTIAL_PROVIDER,
    Account,
    IdentitySession,
    User,
    Verification,
    create_identity_tables,
)
from hexcore.darwin.infrastructure.repositories import (  # noqa: E402
    SqlAlchemyAccountRepository,
    SqlAlchemyAuditSink,
    SqlAlchemySessionRepository,
    SqlAlchemyUserRepository,
    SqlAlchemyVerificationRepository,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def engine():
    """
    `StaticPool` es obligatorio con `:memory:`: sin él cada conexión ve una base vacía.

    Se dispone antes y después, siguiendo el patrón de `tests/test_cron_sql.py`.
    """
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))
    yield motor
    asyncio.run(dispose_engine())


@pytest.fixture
def usuarios(engine):
    return SqlAlchemyUserRepository()


@pytest.fixture
def sesiones(engine):
    return SqlAlchemySessionRepository()


async def _crear_usuario(repo, email="ana@ejemplo.com") -> User:
    return await repo.add(User(email=email))


def _sesion(actor, subject=None, **overrides) -> IdentitySession:
    base = dict(
        actor_user_id=actor,
        subject_user_id=subject or actor,
        token_hash=uuid4().hex,
        expires_at=AHORA + timedelta(minutes=30),
    )
    base.update(overrides)
    return IdentitySession(**base)


# ── Usuarios ──────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_alta_y_lectura_por_mail(usuarios):
    creado = await _crear_usuario(usuarios)
    leido = await usuarios.get_by_email("ana@ejemplo.com")

    assert leido is not None
    assert leido.id == creado.id
    assert leido.email_verified is False
    assert leido.token_generation == 0


@pytest.mark.anyio
async def test_las_fechas_vuelven_tz_aware(usuarios):
    """
    SQLite no guarda zona horaria, así que un round-trip descuidado devuelve naive y
    compararlo con un `datetime` aware lanza `TypeError` en el primer chequeo de vencimiento.
    """
    creado = await _crear_usuario(usuarios)
    leido = await usuarios.get_by_id(creado.id)

    assert leido is not None
    assert leido.created_at.tzinfo is not None


@pytest.mark.anyio
async def test_mail_duplicado_viola_el_unique(usuarios):
    """El unique va en la base: dos signups concurrentes no pueden crear dos cuentas."""
    await _crear_usuario(usuarios)

    with pytest.raises(IntegrityError):
        await usuarios.add(User(email="ana@ejemplo.com"))


@pytest.mark.anyio
async def test_bump_token_generation_es_atomico(usuarios):
    """
    Se hace con un solo UPDATE. Con leer-sumar-escribir, dos revocaciones masivas
    concurrentes dejarían una sin efecto — y lo que se pierde es "cerrá todas las sesiones".
    """
    creado = await _crear_usuario(usuarios)

    resultados = await asyncio.gather(
        *(usuarios.bump_token_generation(creado.id) for _ in range(10))
    )

    assert sorted(resultados) == list(range(1, 11))
    final = await usuarios.get_by_id(creado.id)
    assert final is not None
    assert final.token_generation == 10


@pytest.mark.anyio
async def test_el_campo_extra_round_trippea(usuarios):
    creado = await usuarios.add(
        User(email="b@ejemplo.com", extra={"plan": "pro", "tour": True})
    )
    leido = await usuarios.get_by_id(creado.id)

    assert leido is not None
    assert leido.extra == {"plan": "pro", "tour": True}


# ── Sesiones ──────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_una_sesion_guarda_los_dos_principales(usuarios, sesiones):
    operador = await _crear_usuario(usuarios, "op@ejemplo.com")
    cliente = await _crear_usuario(usuarios, "cli@ejemplo.com")

    creada = await sesiones.add(
        _sesion(
            operador.id,
            cliente.id,
            impersonation_reason="ticket #1",
            impersonation_granted_by=operador.id,
        )
    )
    leida = await sesiones.get(creada.id)

    assert leida is not None
    assert leida.actor_user_id == operador.id
    assert leida.subject_user_id == cliente.id
    assert leida.is_impersonated is True
    assert leida.impersonation_reason == "ticket #1"


@pytest.mark.anyio
async def test_se_busca_por_hash_no_por_token(usuarios, sesiones):
    usuario = await _crear_usuario(usuarios)
    creada = await sesiones.add(_sesion(usuario.id, token_hash="a" * 64))

    encontrada = await sesiones.get_by_token_hash("a" * 64)

    assert encontrada is not None
    assert encontrada.id == creada.id


@pytest.mark.anyio
async def test_dos_sesiones_no_pueden_compartir_token(usuarios, sesiones):
    usuario = await _crear_usuario(usuarios)
    await sesiones.add(_sesion(usuario.id, token_hash="b" * 64))

    with pytest.raises(IntegrityError):
        await sesiones.add(_sesion(usuario.id, token_hash="b" * 64))


@pytest.mark.anyio
async def test_consume_for_rotation_deja_pasar_exactamente_uno(usuarios, sesiones):
    """
    **El test más importante de la fase.**

    Diez refresh concurrentes con el mismo token: exactamente uno tiene que ganar y nueve
    recibir `None`. Ese `None` es lo que dispara la detección de reuso. Si pasaran dos, un
    refresh robado sería indistinguible de uno legítimo y el mecanismo entero sobraría.
    """
    usuario = await _crear_usuario(usuarios)
    creada = await sesiones.add(_sesion(usuario.id))

    resultados = await asyncio.gather(
        *(sesiones.consume_for_rotation(creada.id, at=AHORA) for _ in range(10))
    )

    ganadores = [r for r in resultados if r is not None]
    assert len(ganadores) == 1
    assert len(resultados) - len(ganadores) == 9


@pytest.mark.anyio
async def test_una_sesion_revocada_no_se_puede_consumir(usuarios, sesiones):
    usuario = await _crear_usuario(usuarios)
    creada = await sesiones.add(_sesion(usuario.id))
    await sesiones.revoke(creada.id, at=AHORA, reason="logout")

    assert await sesiones.consume_for_rotation(creada.id, at=AHORA) is None


@pytest.mark.anyio
async def test_revoke_family_cae_todo_el_linaje(usuarios, sesiones):
    """Ante un reuso: revocar una sola dejaría al otro portador adentro."""
    usuario = await _crear_usuario(usuarios)
    familia = uuid4()
    for _ in range(3):
        await sesiones.add(_sesion(usuario.id, family_id=familia))
    # Otra familia, que no debe tocarse.
    await sesiones.add(_sesion(usuario.id))

    revocadas = await sesiones.revoke_family(familia, at=AHORA, reason="reuso")

    assert revocadas == 3
    vivas = await sesiones.list_active_for_user(usuario.id)
    assert len(vivas) == 1


@pytest.mark.anyio
async def test_revoke_es_idempotente(usuarios, sesiones):
    usuario = await _crear_usuario(usuarios)
    creada = await sesiones.add(_sesion(usuario.id))

    await sesiones.revoke(creada.id, at=AHORA, reason="logout")
    await sesiones.revoke(creada.id, at=AHORA + timedelta(minutes=5), reason="otra vez")

    leida = await sesiones.get(creada.id)
    assert leida is not None
    # El `WHERE revoked_at IS NULL` evita que la segunda pise la marca original.
    assert leida.revoked_at is not None
    assert leida.revoked_at.replace(tzinfo=UTC) == AHORA


@pytest.mark.anyio
async def test_list_active_ignora_las_revocadas(usuarios, sesiones):
    usuario = await _crear_usuario(usuarios)
    viva = await sesiones.add(_sesion(usuario.id))
    muerta = await sesiones.add(_sesion(usuario.id))
    await sesiones.revoke(muerta.id, at=AHORA, reason="logout")

    activas = await sesiones.list_active_for_user(usuario.id)

    assert [s.id for s in activas] == [viva.id]


@pytest.mark.anyio
async def test_delete_expired_barre_las_vencidas(usuarios, sesiones):
    usuario = await _crear_usuario(usuarios)
    await sesiones.add(_sesion(usuario.id, expires_at=AHORA - timedelta(days=1)))
    await sesiones.add(_sesion(usuario.id, expires_at=AHORA + timedelta(days=1)))

    borradas = await sesiones.delete_expired(before=AHORA)

    assert borradas == 1


# ── Cuentas ───────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_la_credencial_es_una_cuenta_mas(usuarios, engine):
    """
    Diseño de Better Auth: la contraseña vive en `account`, no en `user`. Por eso agregar
    OAuth no necesita esquema nuevo, sólo filas nuevas.
    """
    cuentas = SqlAlchemyAccountRepository()
    usuario = await _crear_usuario(usuarios)

    await cuentas.add(
        Account(
            user_id=usuario.id,
            provider_id=CREDENTIAL_PROVIDER,
            account_id=str(usuario.id),
            password="$argon2id$fake",
        )
    )
    await cuentas.add(
        Account(user_id=usuario.id, provider_id="google", account_id="g-123")
    )

    credencial = await cuentas.get_credential(usuario.id)
    assert credencial is not None
    assert credencial.password == "$argon2id$fake"
    assert len(await cuentas.list_for_user(usuario.id)) == 2


@pytest.mark.anyio
async def test_una_cuenta_externa_no_se_puede_linkear_dos_veces(usuarios, engine):
    """El constraint que hace segura la vinculación OAuth."""
    cuentas = SqlAlchemyAccountRepository()
    uno = await _crear_usuario(usuarios, "uno@ejemplo.com")
    dos = await _crear_usuario(usuarios, "dos@ejemplo.com")

    await cuentas.add(Account(user_id=uno.id, provider_id="google", account_id="g-1"))

    with pytest.raises(IntegrityError):
        await cuentas.add(
            Account(user_id=dos.id, provider_id="google", account_id="g-1")
        )


# ── Verificaciones ────────────────────────────────────────────────────────────
@pytest.fixture
def verificaciones(engine):
    return SqlAlchemyVerificationRepository()


def _verificacion(**overrides) -> Verification:
    base = dict(
        identifier="ana@ejemplo.com",
        value_hash="h" * 64,
        purpose="email_verification",
        expires_at=AHORA + timedelta(minutes=15),
    )
    base.update(overrides)
    return Verification(**base)


@pytest.mark.anyio
async def test_un_token_de_verificacion_se_consume_una_sola_vez(verificaciones):
    """"De un solo uso" tiene que ser cierto bajo concurrencia, no sólo en el camino feliz."""
    await verificaciones.add(_verificacion())

    resultados = await asyncio.gather(
        *(
            verificaciones.consume(
                "ana@ejemplo.com", "email_verification", "h" * 64, at=AHORA
            )
            for _ in range(10)
        )
    )

    assert len([r for r in resultados if r is not None]) == 1


@pytest.mark.anyio
async def test_un_token_no_sirve_para_otro_proposito(verificaciones):
    """Un código de reset de contraseña no se canjea en el flujo de verificar el mail."""
    await verificaciones.add(_verificacion(purpose="password_reset"))

    consumido = await verificaciones.consume(
        "ana@ejemplo.com", "email_verification", "h" * 64, at=AHORA
    )

    assert consumido is None


@pytest.mark.anyio
async def test_un_token_vencido_no_se_consume(verificaciones):
    await verificaciones.add(_verificacion(expires_at=AHORA - timedelta(minutes=1)))

    assert (
        await verificaciones.consume(
            "ana@ejemplo.com", "email_verification", "h" * 64, at=AHORA
        )
        is None
    )


@pytest.mark.anyio
async def test_invalidate_for_mata_los_pendientes(verificaciones):
    """
    Se llama al emitir uno nuevo: sin esto, cincuenta clicks en "reenviar" dejan cincuenta
    códigos válidos y el espacio a adivinar se multiplica por cincuenta.
    """
    for _ in range(3):
        await verificaciones.add(_verificacion(value_hash=uuid4().hex))

    invalidados = await verificaciones.invalidate_for(
        "ana@ejemplo.com", "email_verification", at=AHORA
    )

    assert invalidados == 3


@pytest.mark.anyio
async def test_increment_attempts_pone_techo_a_la_fuerza_bruta(verificaciones):
    creado = await verificaciones.add(_verificacion())

    assert await verificaciones.increment_attempts(creado.id) == 1
    assert await verificaciones.increment_attempts(creado.id) == 2


# ── Auditoría ─────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_la_auditoria_se_escribe_en_la_transaccion_del_llamador(engine):
    """
    **La razón de que `AbstractAuditSink` sea un puerto aparte del bus de eventos.**

    La propiedad que importa: `record()` **no commitea**, así que la fila vive o muere con la
    transacción del llamador. Si el cambio que la auditoría registra se rollbackea, el
    registro se va con él; si se confirma, queda. No puede haber una sin la otra.

    Se verifica con un rollback y no mirando "¿se ve antes del commit?": el fixture usa
    SQLite `:memory:` con `StaticPool`, o sea **una sola conexión compartida**, así que una
    segunda sesión ve el flush de la primera y esa pregunta no se puede hacer acá.
    """
    from sqlalchemy import func, select

    from hexcore.darwin.infrastructure.models import AuditLogModel
    from hexcore.infrastructure.uow.scopes import session_scope

    async def contar() -> int:
        async with session_scope() as sesion:
            resultado = await sesion.execute(
                select(func.count()).select_from(AuditLogModel)
            )
            return int(resultado.scalar_one())

    assert await contar() == 0

    # Rollback: la fila de auditoría se va con la transacción.
    async with session_scope() as sesion:
        sink = SqlAlchemyAuditSink(session=sesion)
        await sink.record(
            action="impersonation.start", actor_id=uuid4(), subject_id=uuid4()
        )
        await sesion.rollback()

    assert await contar() == 0, (
        "la auditoría sobrevivió a un rollback: se está escribiendo en su propia "
        "transacción y puede quedar sin el cambio que registra"
    )

    # Commit: queda.
    async with session_scope() as sesion:
        sink = SqlAlchemyAuditSink(session=sesion)
        await sink.record(
            action="impersonation.start",
            actor_id=uuid4(),
            subject_id=uuid4(),
            impersonated=True,
            metadata={"ticket": "4821"},
        )
        await sesion.commit()

    assert await contar() == 1


@pytest.mark.anyio
async def test_la_auditoria_acepta_un_principal_de_sistema(engine):
    """
    `actor_id` es `String` y no FK: un cron no es una fila de `user`, y la auditoría tiene
    que poder registrarlo igual.
    """
    from hexcore.infrastructure.uow.scopes import session_scope

    async with session_scope() as sesion:
        sink = SqlAlchemyAuditSink(session=sesion)
        await sink.record(
            action="register.close",
            actor_id="cron:cerrar-registros",
            subject_id=None,
        )
        await sesion.commit()


# ── Esquema ───────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_create_identity_tables_es_idempotente(engine):
    await create_identity_tables(engine)
    await create_identity_tables(engine)
