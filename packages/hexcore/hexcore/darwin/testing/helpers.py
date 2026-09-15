"""
Los atajos del kit: cablear Darwin sin base, y construir contextos a mano.

Dos grupos de cosas, con dos propósitos distintos:

- `configure_test_identity()` cablea el módulo entero con los fakes en una llamada. Es para probar
  **flujos**: el sign-in, la rotación, un plugin.
- `authenticated_context()` e `impersonated_context()` construyen un `AuthContext` directo, sin
  pasar por ningún flujo. Es para probar **lo que está aguas abajo** de la autenticación: un
  handler que consulta `has_scope`, un caso de uso que lee el `subject_id`. No hay que emitir un
  token para eso, y emitirlo acopla el test a la capa de crypto sin motivo.

⚠️ **`impersonated_context()` existe justamente porque construir uno a mano es fácil de hacer mal.**
`AuthContext` se niega a existir si `subject != actor` sin un permiso de impersonación, así que el
primer intento de todo el mundo falla con un `ValidationError` que hay que leer dos veces. El helper
arma el `Impersonation` completo y coherente.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from hexcore.darwin.domain.context import (
    AuthContext,
    Impersonation,
    Principal,
    SystemPrincipal,
    Transport,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.application.config import IdentityConfig
    from hexcore.darwin.application.container import IdentityContainer
    from hexcore.darwin.domain.entities import User

__all__ = [
    "TEST_SECRET_KEY",
    "authenticated_context",
    "impersonated_context",
    "system_context",
    "configure_test_identity",
    "make_user",
    "create_test_user",
]

#: La clave de firma de los tests.
#:
#: Fija y no aleatoria para que un token emitido en un test se pueda verificar en otro, y **larga**
#: porque `IdentityConfig` valida el mínimo — una clave corta falla al construir la config y el
#: error apunta a la config y no acá.
TEST_SECRET_KEY = "clave-de-test-que-no-se-usa-en-produccion-nunca-jamas"


def make_user(
    email: str = "test@ejemplo.com",
    *,
    user_id: UUID | None = None,
    verified: bool = True,
    name: str | None = None,
    scopes: t.Sequence[str] = (),
    **extra: t.Any,
) -> "User":
    """
    Un `User` listo para usar.

    `verified=True` por default porque el caso de "mail sin verificar" es el excepcional y
    escribirlo en cada test es ruido. Los `scopes` van a `extra`, que es donde los lee la política
    de `impersonate` y donde el consumidor pone su modelo de autorización.

    Uso::

        from hexcore.darwin.testing import make_user

        ana = make_user("ana@ejemplo.com", scopes=["admin"])
    """
    from hexcore.darwin.domain.entities import User

    contenido: dict[str, t.Any] = dict(extra)
    if scopes:
        contenido["scopes"] = list(scopes)

    return User(
        id=user_id or uuid4(),
        email=email,
        email_verified=verified,
        name=name,
        extra=contenido,
    )


# ── Contextos ─────────────────────────────────────────────────────────────────
def authenticated_context(
    user: "User | UUID",
    *,
    scopes: t.Iterable[str] = (),
    roles: t.Iterable[str] = (),
    session_id: UUID | None = None,
    transport: Transport = "bearer",
) -> AuthContext[t.Any]:
    """
    Un `AuthContext` normal: actor y subject son la misma persona.

    Args:
        user: El `User`, o su id. Aceptar el id suelto es para los tests que no necesitan la
            entidad — que son la mayoría de los que sólo miran `actor_id`.
        scopes, roles: Los del **actor**, que es de donde `has_scope` y `has_role` los leen.
        session_id: El `sid`. Uno aleatorio si no viene.
        transport: `"bearer"` por default y no `"cookie"`, porque el chequeo anti-CSRF sólo
            aplica a cookie y un test que no lo está probando no debería tener que saltearlo.

    Uso::

        from hexcore.darwin import auth_scope
        from hexcore.darwin.testing import authenticated_context, make_user

        ctx = authenticated_context(make_user(), scopes=["facturas:leer"])
        with auth_scope(ctx):
            resultado = await mi_caso_de_uso()
    """
    principal = _principal(user, scopes=scopes, roles=roles, session_id=session_id)
    return AuthContext(actor=principal, subject=principal, transport=transport)


def impersonated_context(
    actor: "User | UUID",
    subject: "User | UUID",
    *,
    reason: str = "test",
    scopes: t.Iterable[str] = (),
    roles: t.Iterable[str] = (),
    session_id: UUID | None = None,
    granted_at: datetime | None = None,
    duration: timedelta = timedelta(minutes=60),
    transport: Transport = "bearer",
) -> AuthContext[t.Any]:
    """
    Un `AuthContext` impersonado, con el permiso completo y coherente.

    ⚠️ **Existe porque armarlo a mano es fácil de hacer mal.** `AuthContext` se niega a existir si
    `subject != actor` sin `Impersonation`, y el `Impersonation` a su vez exige `reason` no vacío y
    `expires_at > granted_at`. El primer intento de todo el mundo falla con un `ValidationError`
    que hay que leer dos veces — y esa fricción es deliberada en producción y ruido en un test.

    Args:
        actor: Quién ejecuta.
        subject: A quién afecta.
        reason: El motivo. No vacío, porque el modelo lo exige.
        scopes, roles: Los del **actor**. Impersonar no presta permisos: si le pasás los del
            subject, tu test estaría probando algo que Darwin no hace.
        duration: Cuánto dura. 60 min, que es `IMPERSONATION_CAP`.

    Uso::

        from hexcore.darwin.testing import impersonated_context, make_user

        soporte, cliente = make_user("soporte@x.com"), make_user("cliente@x.com")
        ctx = impersonated_context(soporte, cliente, reason="ticket #4821")

        assert ctx.is_impersonating
        assert ctx.actor_id == soporte.id
        assert ctx.subject_id == cliente.id
    """
    sid = session_id or uuid4()
    desde = granted_at or datetime.now(UTC)

    actor_principal = _principal(actor, scopes=scopes, roles=roles, session_id=sid)
    subject_principal = _principal(subject, session_id=sid)

    if actor_principal.user_id == subject_principal.user_id:
        # El validador del modelo lo rechazaría igual, pero con un mensaje sobre el invariante.
        # Acá el problema es el uso del helper, y el mensaje lo dice.
        raise ValueError(
            "`impersonated_context` necesita un actor y un subject distintos. Para una sesión "
            "normal usá `authenticated_context`."
        )

    return AuthContext(
        actor=actor_principal,
        subject=subject_principal,
        transport=transport,
        impersonation=Impersonation(
            granted_by=actor_principal.user_id,
            reason=reason,
            granted_at=desde,
            expires_at=desde + duration,
        ),
    )


def system_context(
    name: str = "test:proceso",
    *,
    scopes: t.Iterable[str] = (),
) -> AuthContext[t.Any]:
    """
    Un `AuthContext` de proceso automático: un cron, un seed, la CLI.

    `transport="worker"` fijo: es lo que distingue un job de background de un request, y el código
    que ramifica por transporte —el chequeo anti-CSRF, por ejemplo— tiene que poder verlo.

    **No es un superusuario**: lleva la lista de grants que le pases y nada más. Es el mismo
    diseño que `SystemPrincipal`, y el motivo por el que no hay un `is_superuser`.

    Uso::

        ctx = system_context("cron:cerrar-registros", scopes=["register.close"])
        assert ctx.is_system
        assert not ctx.has_scope("usuarios:borrar")
    """
    principal = SystemPrincipal(name=name, scopes=frozenset(scopes))
    return AuthContext(actor=principal, subject=principal, transport="worker")


def _principal(
    quien: "User | UUID",
    *,
    scopes: t.Iterable[str] = (),
    roles: t.Iterable[str] = (),
    session_id: UUID | None = None,
) -> Principal:
    """
    Un `Principal` desde un `User` o desde un id.

    El mail se copia si viene un `User`, porque hay código —los mensajes de auditoría, el QR de
    TOTP— que lo usa para mostrar algo, y un `None` ahí produce un test que pasa con una etiqueta
    vacía.
    """
    if isinstance(quien, UUID):
        return Principal(
            user_id=quien,
            session_id=session_id or uuid4(),
            scopes=frozenset(scopes),
            roles=frozenset(roles),
        )

    return Principal(
        user_id=quien.id,
        session_id=session_id or uuid4(),
        email=quien.email,
        scopes=frozenset(scopes),
        roles=frozenset(roles),
    )


# ── Cableado ──────────────────────────────────────────────────────────────────
def configure_test_identity(
    config: "IdentityConfig | None" = None,
    *,
    seed_users: t.Iterable["User"] = (),
    clock: t.Any = None,
    now: datetime | None = None,
    plugins: t.Any = None,
    audit: t.Any = None,
    **overrides: t.Any,
) -> "IdentityContainer":
    """
    Cablea Darwin con los fakes, sin base y sin crypto lenta. En una llamada.

    Qué inyecta, y por qué cada uno:

    - Los cinco repositorios en memoria, así que **no hace falta motor ni tablas**.
    - `PlainTextHasher`, porque Argon2id tarda ~100 ms a propósito y cincuenta sign-ins en una
      suite son cinco segundos de KDF.
    - Un `FixedClock`, para que los tests de vencimiento no dependan del reloj de la máquina.
    - Una clave de firma **real** (`StaticKeyStore` con una Ed25519 generada): los tokens que
      salen son tokens de verdad y verifican de verdad. Falsear la firma haría que un test de
      confusión de `alg` no pruebe nada.

    Args:
        config: La `IdentityConfig`. Por defecto una con `TEST_SECRET_KEY` y
            `require_verified_email=False` — el caso de "mail sin verificar" se prueba
            explícitamente, no por accidente en cada test.
        seed_users: Usuarios a sembrar. ⚠️ **Quedan sin contraseña**, así que no pueden hacer
            `sign_in` — es el estado correcto de quien entra sólo por OAuth o por passkey. Para
            un usuario que sí pueda, usá `create_test_user`, que pasa por el `sign_up` real.

            Se llama `seed_users` y no `users` a propósito: `users=` es la clave del **puerto**
            en `**overrides`, igual que en `configure_identity`. Con un solo nombre para las dos
            cosas, pasar un repositorio propio sembraba un repositorio como si fuera una lista de
            usuarios, y el error salía tres capas abajo. Lo encontró un test.
        clock: Un reloj propio. Si no viene, un `FixedClock` en `now`.
        now: El instante inicial del reloj. Por defecto, uno fijo y reproducible.
        plugins: Un `PluginRegistry`.
        audit: Un sink. Pasá `RecordingAuditSink()` cuando el test asevere sobre la auditoría.
        **overrides: Cualquier puerto, para reemplazar uno de los fakes.

    ⚠️ **Llamá `reset_identity()` al terminar.** El contenedor es global del proceso, así que un
    test que no limpia le deja el cableado al siguiente. El fixture `identity_container` de
    `hexcore.darwin.testing.fixtures` lo hace solo.

    Uso::

        from hexcore.darwin import reset_identity
        from hexcore.darwin.testing import configure_test_identity, make_user

        contenedor = configure_test_identity(seed_users=[make_user("ana@ejemplo.com")])
        try:
            servicio = contenedor.identity_service()
        finally:
            reset_identity()
    """
    from pydantic import SecretStr

    from hexcore.darwin.application.config import IdentityConfig, TokenConfig
    from hexcore.darwin.application.container import configure_identity
    from hexcore.darwin.infrastructure.clock import FixedClock
    from hexcore.darwin.infrastructure.keys import StaticKeyStore, generate_signing_key
    from hexcore.darwin.testing.fakes import (
        FakeAccountRepository,
        FakeRevocationList,
        FakeSessionRepository,
        FakeUserRepository,
        FakeVerificationRepository,
        PlainTextHasher,
    )

    reloj = clock or FixedClock(now or datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    componentes: dict[str, t.Any] = {
        "users": FakeUserRepository(seed_users),
        "sessions": FakeSessionRepository(),
        "accounts": FakeAccountRepository(),
        "verifications": FakeVerificationRepository(),
        # El reloj se le pasa a la denylist: sin eso usaría `datetime.now(UTC)` y un test con
        # `FixedClock` vería vencimientos incoherentes.
        "revocations": FakeRevocationList(clock=reloj),
        "hasher": PlainTextHasher(),
        "clock": reloj,
        "key_store": StaticKeyStore([generate_signing_key(kid="test")]),
    }
    if plugins is not None:
        componentes["plugins"] = plugins
    if audit is not None:
        componentes["audit"] = audit
    componentes.update(overrides)

    return configure_identity(
        config
        or IdentityConfig(
            # `SecretStr` explícito: pydantic coerce el `str` en runtime, pero el tipo declarado
            # es `SecretStr | None` y pasar el `str` desnudo deja al checker señalando el
            # llamador — que en este caso es el kit que todos van a copiar.
            secret_key=SecretStr(TEST_SECRET_KEY),
            tokens=TokenConfig(issuer="https://test.local"),
            require_verified_email=False,
        ),
        **componentes,
    )


async def create_test_user(
    container: "IdentityContainer",
    email: str = "test@ejemplo.com",
    password: str = "una frase larga y buena",
    *,
    verified: bool = True,
    scopes: t.Sequence[str] = (),
    **extra: t.Any,
) -> "User":
    """
    Crea un usuario **con contraseña**, pasando por el `sign_up` real.

    Por el flujo real y no sembrando la fila a mano: el `sign_up` es el que crea la `Account` del
    provider `credential` con el hash adentro, y sembrar sólo el `User` deja alguien que existe y
    no puede iniciar sesión. Es el error más común al usar el kit, y por eso hay una función.

    `verified=True` por default marca el mail como verificado sin canjear el código: el flujo de
    verificación se prueba aparte, y hacerlo en cada test es ruido.

    Args:
        container: El contenedor de `configure_test_identity`.
        email, password: Las credenciales.
        verified: Si se marca el mail verificado.
        scopes: Van a `extra["scopes"]`, que es donde los lee la política de `impersonate`.
        **extra: Más contenido para `extra`.

    Uso::

        from hexcore.darwin.testing import configure_test_identity, create_test_user

        contenedor = configure_test_identity()
        ana = await create_test_user(contenedor, "ana@ejemplo.com")
        _, _, par = await contenedor.identity_service().sign_in(
            email="ana@ejemplo.com", password="una frase larga y buena"
        )
    """
    usuario, _ = await container.identity_service().sign_up(
        email=email, password=password
    )

    contenido: dict[str, t.Any] = dict(usuario.extra)
    contenido.update(extra)
    if scopes:
        contenido["scopes"] = list(scopes)

    cambios: dict[str, t.Any] = {"extra": contenido}
    if verified:
        cambios["email_verified"] = True

    return await container.users().update(usuario.model_copy(update=cambios))
