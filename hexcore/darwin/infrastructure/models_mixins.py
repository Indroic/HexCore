"""
Mixins de las tablas de identidad. **Importar este módulo no registra ninguna tabla.**

Esa propiedad es el centro del diseño y no un detalle: las clases de acá no heredan de
`Base` y no declaran `__tablename__`, así que `Base.metadata` queda intacto al importarlas.
Es lo que permite que **el consumidor** declare las clases concretas en su propio paquete
``models/``, donde `import_all_models` las ve — y por lo tanto `alembic revision
--autogenerate` también.

Por qué importa tanto: si las tablas del framework se registraran solas pero el `env.py` del
consumidor no las importara, `--autogenerate` las vería ausentes de `Base.metadata` y
emitiría ``op.drop_table``. Con Darwin eso sería el almacén de credenciales completo,
borrado por una migración de rutina.

Dos reglas heredadas de `hexcore.infrastructure.cqrs.cron_sql`, y las dos tienen test:

1. **No heredan `BaseModel[T]`.** `BaseModel.get_domain_entity()` devuelve
   `self._domain_entity` sin default, y `SqlAlchemyUnitOfWork.collect_domain_entities()` lo
   llama para todo `BaseModel` que la sesión tenga trackeado. Una fila insertada sin
   `set_domain_entity()` hace explotar `commit()` **después** de que la transacción ya se
   confirmó — y ni `commit()` ni `__aexit__` rollbackean. Resultado: fila persistida, 500 al
   usuario.
2. Los repositorios tampoco heredan `BaseSQLAlchemyRepository` (ver `repositories.py`).

Uso, en el paquete del consumidor::

    # myapp/models/identity.py
    from hexcore.darwin import UserMixin, SessionMixin
    from hexcore.sql import Base

    class User(UserMixin, Base):
        __tablename__ = "darwin_user"
        plan: Mapped[str] = mapped_column(String(32), default="free")   # extensión propia

    class Session(SessionMixin, Base):
        __tablename__ = "darwin_session"
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

__all__ = [
    "DEFAULT_USER_TABLE",
    "DEFAULT_SESSION_TABLE",
    "DEFAULT_ACCOUNT_TABLE",
    "DEFAULT_VERIFICATION_TABLE",
    "DEFAULT_AUDIT_TABLE",
    "DEFAULT_JWKS_TABLE",
    "JSON_PORTABLE",
    "TimestampMixin",
    "UserMixin",
    "SessionMixin",
    "AccountMixin",
    "VerificationMixin",
    "AuditLogMixin",
    "JwksMixin",
]

DEFAULT_USER_TABLE = "darwin_user"
DEFAULT_SESSION_TABLE = "darwin_session"
DEFAULT_ACCOUNT_TABLE = "darwin_account"
DEFAULT_VERIFICATION_TABLE = "darwin_verification"
DEFAULT_AUDIT_TABLE = "darwin_audit_log"
DEFAULT_JWKS_TABLE = "darwin_jwks"

#: `JSONB` nativo en PostgreSQL, `JSON` portable en el resto. `JSONB` a secas rompe la
#: suite, que corre sobre el fixture `sqlite_engine` de `hexcore.testing.fixtures`.
JSON_PORTABLE = JSON().with_variant(JSONB(), "postgresql")


def _ahora() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """`created_at` / `updated_at` tz-aware. Separado para que cada tabla lo componga."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora, onupdate=_ahora
    )


class UserMixin(TimestampMixin):
    """Columnas de `user`. Port de Better Auth."""

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker
    #: sepa que existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones
    #: `ClassVar`, así que no lo interpreta como columna.
    __tablename__: t.ClassVar[str]

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    #: Fuera de Better Auth. Revoca todas las sesiones del usuario con **un** UPDATE, sin
    #: importar cuántas tenga: el token lleva `gen` y la verificación compara. La
    #: alternativa —recorrer y revocar de a una— es O(n) y no es atómica.
    token_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Bloqueo temporal. Distinto de `is_active`: una cuenta bloqueada existe y vuelve, una
    #: desactivada se fue. Mezclarlas hace que desbloquear y reactivar sean lo mismo.
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @declared_attr
    def extra(cls) -> Mapped[dict[str, t.Any]]:  # noqa: N805
        # `declared_attr` porque un default mutable tiene que crearse por clase.
        return mapped_column(JSON_PORTABLE, nullable=False, default=dict)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        # Derivado del `__tablename__` de la clase concreta, que el mixin todavía no
        # conoce. Un nombre fijo rompería en cuanto alguien renombre la tabla.
        return (
            UniqueConstraint("email", name=f"uq_{cls.__tablename__}_email"),
            Index(f"ix_{cls.__tablename__}_created_at", "created_at"),
        )


class SessionMixin(TimestampMixin):
    """
    Columnas de `session`, con **el desvío más importante frente a Better Auth**.

    Better Auth tiene un solo `userId` más un `impersonatedBy` opcional. Acá hay **dos
    principales y ninguno es opcional**: con los dos siempre presentes, toda fila escrita
    por la sesión es atribuible sin ambigüedad. Con un id y un flag, reconstruir quién hizo
    qué depende de que el flag se setee bien en todos los caminos, y ese es exactamente el
    invariante que se rompe en el camino menos transitado.
    """

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker
    #: sepa que existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones
    #: `ClassVar`, así que no lo interpreta como columna.
    __tablename__: t.ClassVar[str]

    #: Nombre de la tabla de usuarios a la que apuntan las FKs. Se sobreescribe en la clase
    #: concreta si renombrás `user`, porque una FK no se puede derivar sola.
    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def actor_user_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        """Quién EJECUTA. Nunca se deduce, nunca se hereda."""
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    @declared_attr
    def subject_user_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        """A quién AFECTA. En una sesión normal, el mismo actor."""
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    #: **SHA-256 del token, nunca el token.** Better Auth lo guarda en claro; un dump de
    #: esta tabla sería entonces un set de credenciales de sesión utilizables. No se usa
    #: Argon2: el token es aleatorio de 256 bits, no una contraseña, así que no hay
    #: diccionario del que defenderse.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Linaje de rotación de refresh: un reuso revoca la familia entera.
    family_id: Mapped[PythonUUID] = mapped_column(nullable=False, default=uuid4)

    #: Atado al `aud` del token: impide replayear una cookie como Bearer y esquivar CSRF.
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="cookie")

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    #: Redundante con `actor != subject` a propósito: el motivo y quién autorizó no se
    #: pueden deducir de los ids, y sin ellos la auditoría no responde nada.
    impersonation_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    impersonation_granted_by: Mapped[PythonUUID | None] = mapped_column(nullable=True)
    impersonation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            # Único: un token no puede pertenecer a dos sesiones.
            UniqueConstraint("token_hash", name=f"uq_{nombre}_token_hash"),
            # "listar mis sesiones" y "cerrar todas".
            Index(f"ix_{nombre}_subject_revoked", "subject_user_id", "revoked_at"),
            # Auditoría: "qué hizo este operador".
            Index(f"ix_{nombre}_actor", "actor_user_id"),
            # El reaper barre por vencimiento.
            Index(f"ix_{nombre}_expires_at", "expires_at"),
            # Revocación de familia ante reuso de refresh.
            Index(f"ix_{nombre}_family", "family_id"),
        )


class AccountMixin(TimestampMixin):
    """
    Columnas de `account`: OAuth y la credencial local.

    La contraseña vive acá y no en `user`. Es el diseño de Better Auth y es el correcto: un
    usuario puede tener cero contraseñas (entra sólo con Google) o cambiar de método sin
    tocar su fila.
    """

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker
    #: sepa que existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones
    #: `ClassVar`, así que no lo interpreta como columna.
    __tablename__: t.ClassVar[str]

    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def user_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: El id del usuario **en el proveedor**. Para la credencial local, el propio user_id.
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Hash de Argon2id, sólo con `provider_id == "credential"`.
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Tokens de terceros. Los cifra la capa de crypto (Fase 4): son credenciales de otro
    #: sistema, y filtrarlas es un incidente en la API de un tercero además del propio.
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    id_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            # El constraint que hace segura la vinculación OAuth: la misma cuenta de Google
            # no se puede linkear a dos usuarios.
            UniqueConstraint(
                "provider_id", "account_id", name=f"uq_{nombre}_provider_account"
            ),
            Index(f"ix_{nombre}_user", "user_id"),
        )


class VerificationMixin(TimestampMixin):
    """
    Columnas de `verification`: tokens de un solo uso.

    Tres desvíos frente a Better Auth, todos para que la tabla no sea utilizable si se
    filtra y para que la fuerza bruta tenga techo: se guarda `value_hash` y no el código,
    `purpose` es parte de la identidad del token, y `attempts` pone techo (un OTP de 6
    dígitos son 10^6 combinaciones, que sin límite se agotan en minutos).
    """

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker
    #: sepa que existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones
    #: `ClassVar`, así que no lo interpreta como columna.
    __tablename__: t.ClassVar[str]

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)
    identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            Index(f"ix_{nombre}_identifier_purpose", "identifier", "purpose"),
            Index(f"ix_{nombre}_expires_at", "expires_at"),
        )


class AuditLogMixin:
    """
    Columnas de `audit_log`. Tabla nueva, no está en Better Auth.

    Existe aparte del bus de eventos a propósito: los eventos son *notificaciones* y pueden
    perderse, reordenarse o procesarse en otro proceso. La auditoría de una impersonación
    tiene que escribirse **en la misma transacción** que el cambio que registra, o existe la
    ventana donde la acción ocurrió y el registro no.

    `actor_id` y `subject_id` son `String` y no FK: un principal de sistema
    (``"cron:cerrar-registros"``) no es una fila de `user`, y la auditoría tiene que poder
    registrarlo igual. Además una FK con CASCADE borraría el registro al borrar el usuario,
    que es justo lo contrario de lo que una auditoría debe hacer.
    """

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker
    #: sepa que existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones
    #: `ClassVar`, así que no lo interpreta como columna.
    __tablename__: t.ClassVar[str]

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    impersonated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Correlación con el `REQUEST_ID` de la capa HTTP, para cruzar logs con auditoría.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora
    )

    @declared_attr
    def audit_metadata(cls) -> Mapped[dict[str, t.Any]]:  # noqa: N805
        # No se llama `metadata`: `Base.metadata` es el `MetaData` de SQLAlchemy y una
        # columna con ese nombre lo pisa en la clase declarativa.
        return mapped_column(JSON_PORTABLE, nullable=False, default=dict)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            Index(f"ix_{nombre}_actor_occurred", "actor_id", "occurred_at"),
            Index(f"ix_{nombre}_subject_occurred", "subject_id", "occurred_at"),
        )


class JwksMixin:
    """
    Columnas de `jwks`: las claves de firma y su estado de rotación.

    Tres estados: ``active`` (firma y verifica), ``verify_only`` (sólo verifica),
    ``retired``. Rotar es publicar la nueva a los verificadores, esperar, cambiar el
    firmante, y retirar la vieja recién después del TTL máximo — así ningún token en vuelo
    queda sin clave que lo verifique.
    """

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker
    #: sepa que existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones
    #: `ClassVar`, así que no lo interpreta como columna.
    __tablename__: t.ClassVar[str]

    kid: Mapped[str] = mapped_column(String(64), primary_key=True)
    algorithm: Mapped[str] = mapped_column(String(16), nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: Cifrada en reposo por la capa de crypto (Fase 4).
    private_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        return (Index(f"ix_{cls.__tablename__}_status", "status"),)
