"""
Los documentos Beanie de identidad. Requiere el extra `[darwin-beanie]`.

⚠️ **No heredan de `hexcore.sql`-style `BaseDocument`, y esta vez la razón es de seguridad.** El
`BaseDocument` del framework declara dos cosas que acá no se pueden aceptar:

1. **`use_cache = True`**, con expiración de 10 minutos. Un documento de `session` leído del cache
   diría que la sesión sigue viva hasta diez minutos después de revocarla — o sea que cerrar sesión
   no tendría efecto, que es exactamente lo que la revocación en tres capas existe para evitar. No
   es una preferencia de rendimiento: es un defecto.
2. **`is_root = True`**, que en Beanie significa *herencia de una sola colección*: todas las
   subclases comparten un `collection`. Usuarios, sesiones y cuentas terminarían en la misma.

Así que heredan `Document` directo y declaran su propio `Settings`. Es el análogo exacto de la regla
"no heredar `BaseModel[T]`" del backend de SQL, y por un motivo más filoso.

**Y a diferencia de SQL, acá los documentos son concretos y no mixins.** El patrón mixin-first
existe en el otro backend por una razón que en Mongo no aplica: que el consumidor declare la clase
en su paquete ``models/`` es lo que hace que `alembic revision --autogenerate` la vea y no le emita
un ``op.drop_table``. Mongo no tiene migraciones de esquema ni autogenerate, así que la ceremonia no
compraría nada. Quien necesite extender el usuario **subclasea** y le pasa su documento al
repositorio, que es más simple y se ve en el IDE.

⚠️ Los documentos igual tienen que llegar a `init_beanie`. Ver `schema.py`.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pymongo
from beanie import Document
from pydantic import Field
from pymongo import IndexModel

__all__ = [
    "DEFAULT_COLLECTION_PREFIX",
    "UserDocument",
    "SessionDocument",
    "AccountDocument",
    "VerificationDocument",
    "AuditLogDocument",
    "JwksDocument",
    "IDENTITY_DOCUMENTS",
]

#: El prefijo de las colecciones, para que se agrupen en `show collections` y no choquen con las
#: del consumidor. Es el mismo criterio que `darwin_` en las tablas.
DEFAULT_COLLECTION_PREFIX = "darwin_"


def _ahora() -> datetime:
    """`datetime.now(UTC)` como función, no como valor por default evaluado al importar."""
    return datetime.now(UTC)


class _Base(Document):
    """
    Base común: el id de dominio y los timestamps.

    `entity_id` y no `_id`: el `_id` de Mongo es un `ObjectId` que el dominio no conoce, y mapear
    el UUID ahí obligaría a convertir en cada borde. Es la misma decisión que toma el
    `BaseDocument` del framework, y la única que se le copia.

    ⚠️ El `Settings` de cada subclase **tiene que** declarar `use_cache = False` explícito. Está
    en `False` por default en Beanie, pero declararlo es lo que hace que un `use_cache = True`
    copiado de otro documento se vea como el cambio de comportamiento que es.
    """

    entity_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=_ahora)
    updated_at: datetime = Field(default_factory=_ahora)


class UserDocument(_Base):
    """
    Colección `darwin_user`.

    Para extender: subclaseá y pasale la subclase al repositorio.

        class MiUsuario(UserDocument):
            plan: str = "free"

            class Settings(UserDocument.Settings):
                pass
    """

    email: str
    email_verified: bool = False
    name: str | None = None
    image: str | None = None

    #: El contador de generación: la capa 3 de la revocación. Subirlo invalida todos los tokens
    #: emitidos antes, y por eso se sube con `$inc` y no leyendo-sumando-escribiendo.
    token_generation: int = 0

    locked_until: datetime | None = None
    is_active: bool = True
    extra: dict[str, t.Any] = Field(default_factory=dict)

    class Settings:
        name = f"{DEFAULT_COLLECTION_PREFIX}user"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            # `email` único: es la clave de login, y sin el índice dos altas concurrentes con el
            # mismo mail crean dos cuentas y el login pasa a ser una lotería.
            IndexModel([("email", pymongo.ASCENDING)], unique=True),
        ]


class SessionDocument(_Base):
    """
    Colección `darwin_session`, con **los dos principales**.

    `actor_user_id` y `subject_user_id` separados es lo que hace auditable la impersonación, igual
    que en el backend de SQL.
    """

    #: Quién EJECUTA. Nunca se deduce, nunca se hereda.
    actor_user_id: UUID
    #: A quién AFECTA. En una sesión normal, el mismo actor.
    subject_user_id: UUID

    #: **SHA-256 del token, nunca el token.** Un dump de esta colección no es un set de
    #: credenciales de sesión utilizables.
    token_hash: str

    #: Linaje de rotación: un reuso revoca la familia entera.
    family_id: UUID = Field(default_factory=uuid4)

    #: Atado al `aud` del token: impide replayear una cookie como Bearer.
    transport: str = "cookie"

    expires_at: datetime
    revoked_at: datetime | None = None
    consumed_at: datetime | None = None

    ip_address: str | None = None
    user_agent: str | None = None

    impersonation_reason: str | None = None
    impersonation_granted_by: UUID | None = None
    impersonation_expires_at: datetime | None = None

    is_active: bool = True

    class Settings:
        name = f"{DEFAULT_COLLECTION_PREFIX}session"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("token_hash", pymongo.ASCENDING)], unique=True),
            IndexModel([("family_id", pymongo.ASCENDING)]),
            IndexModel([("actor_user_id", pymongo.ASCENDING)]),
            # TTL sobre `expires_at`: Mongo borra las sesiones vencidas solo, así que el
            # `delete_expired` del reaper es una red de contención y no el mecanismo principal.
            # Es una ventaja real del backend de Mongo sobre el de SQL, donde hace falta el cron.
            IndexModel([("expires_at", pymongo.ASCENDING)], expireAfterSeconds=0),
        ]


class AccountDocument(_Base):
    """
    Colección `darwin_account`: OAuth y la credencial local.

    La contraseña vive acá y no en `user`, igual que en SQL: un usuario puede tener cero
    contraseñas —entra sólo con Google— o cambiar de método sin tocar su documento.
    """

    user_id: UUID
    provider_id: str
    #: El id del usuario **en el proveedor**. Para la credencial local, el propio `user_id`.
    account_id: str

    #: Hash de Argon2id, sólo con `provider_id == "credential"`.
    password: str | None = None

    #: Tokens de terceros, **cifrados** por `SecretBox`. Son credenciales de otro sistema.
    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    scope: str | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None

    is_active: bool = True

    class Settings:
        name = f"{DEFAULT_COLLECTION_PREFIX}account"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            # El constraint que hace segura la vinculación OAuth: la misma cuenta de un proveedor
            # no se puede vincular a dos usuarios.
            IndexModel(
                [("provider_id", pymongo.ASCENDING), ("account_id", pymongo.ASCENDING)],
                unique=True,
            ),
            IndexModel([("user_id", pymongo.ASCENDING)]),
        ]


class VerificationDocument(_Base):
    """
    Colección `darwin_verification`: tokens de un solo uso.

    Mismos tres desvíos frente a Better Auth que en SQL: se guarda el hash y no el código,
    `purpose` es parte de la identidad del token, y `attempts` le pone techo a la fuerza bruta.
    """

    identifier: str
    value_hash: str
    purpose: str
    expires_at: datetime
    consumed_at: datetime | None = None
    attempts: int = 0
    is_active: bool = True

    class Settings:
        name = f"{DEFAULT_COLLECTION_PREFIX}verification"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel(
                [("identifier", pymongo.ASCENDING), ("purpose", pymongo.ASCENDING)]
            ),
            # El canje busca por los tres: con el índice compuesto es una lectura de índice.
            IndexModel(
                [
                    ("identifier", pymongo.ASCENDING),
                    ("purpose", pymongo.ASCENDING),
                    ("value_hash", pymongo.ASCENDING),
                ]
            ),
            IndexModel([("expires_at", pymongo.ASCENDING)], expireAfterSeconds=0),
        ]


class AuditLogDocument(_Base):
    """
    Colección `darwin_audit_log`.

    ⚠️ **Sin TTL.** Las otras dos colecciones efímeras lo tienen; la auditoría no puede tenerlo:
    su valor entero es poder responder qué pasó seis meses después. La retención es una decisión
    del consumidor y de su regulación, no un default del framework.

    `actor_id` y `subject_id` son `str` y no `UUID`: un principal de sistema
    (``"cron:cerrar-registros"``) no es un usuario, y la auditoría tiene que poder registrarlo.
    """

    action: str
    actor_id: str | None = None
    subject_id: str | None = None
    impersonated: bool = False
    request_id: str | None = None
    audit_metadata: dict[str, t.Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=_ahora)
    is_active: bool = True

    class Settings:
        name = f"{DEFAULT_COLLECTION_PREFIX}audit_log"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel(
                [("actor_id", pymongo.ASCENDING), ("occurred_at", pymongo.DESCENDING)]
            ),
            IndexModel(
                [("subject_id", pymongo.ASCENDING), ("occurred_at", pymongo.DESCENDING)]
            ),
        ]


class JwksDocument(_Base):
    """
    Colección `darwin_jwks`: las claves de firma y su estado de rotación.

    ⚠️ **La privada se guarda en claro acá.** Es la misma decisión que en SQL, y el motivo es que
    cifrarla con una clave de la aplicación movería el problema sin resolverlo: quien lea la base
    normalmente también lee la configuración. La protección de esta colección es el control de
    acceso al almacén, y el diseño para cuando falla es la rotación — de ahí `status`.
    """

    kid: str
    algorithm: str
    public_key: str
    private_key: str
    #: `active`, `verify_only` o `retired`. El intermedio es lo que permite rotar sin desloguear
    #: a todo el mundo.
    status: str = "active"
    is_active: bool = True

    class Settings:
        name = f"{DEFAULT_COLLECTION_PREFIX}jwks"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("kid", pymongo.ASCENDING)], unique=True),
            IndexModel([("status", pymongo.ASCENDING)]),
        ]


#: Los seis, en orden. Es lo que `init_identity_documents` le pasa a `init_beanie`.
IDENTITY_DOCUMENTS: tuple[type[Document], ...] = (
    UserDocument,
    SessionDocument,
    AccountDocument,
    VerificationDocument,
    AuditLogDocument,
    JwksDocument,
)
