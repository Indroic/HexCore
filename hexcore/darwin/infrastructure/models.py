"""
Modelos concretos por defecto de Darwin. **Importar este módulo SÍ registra las 6 tablas.**

Es la contraparte deliberada de `models_mixins`: acá las clases heredan de `Base` y declaran
`__tablename__`, así que entran en `Base.metadata`.

Cuándo importar cuál:

- **Si extendés el modelo de usuario** (el caso recomendado), declarás tus clases concretas
  en tu propio paquete ``models/`` a partir de los mixins, y **no** importás este módulo.
- **Si te alcanza con el esquema por defecto**, importás éste desde tu paquete ``models/``
  para que `import_all_models` lo alcance y `--autogenerate` vea las tablas.

En los dos casos las tablas tienen que ser visibles desde el `env.py` de Alembic. Si no, la
migración las ve ausentes de `Base.metadata` y emite ``op.drop_table``. `create_identity_tables()`
está en `schema.py` para desarrollo y tests.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.infrastructure.models_mixins import (
    DEFAULT_ACCOUNT_TABLE,
    DEFAULT_AUDIT_TABLE,
    DEFAULT_JWKS_TABLE,
    DEFAULT_SESSION_TABLE,
    DEFAULT_USER_TABLE,
    DEFAULT_VERIFICATION_TABLE,
    AccountMixin,
    AuditLogMixin,
    JwksMixin,
    SessionMixin,
    UserMixin,
    VerificationMixin,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

__all__ = [
    "UserModel",
    "SessionModel",
    "AccountModel",
    "VerificationModel",
    "AuditLogModel",
    "JwksModel",
    "IDENTITY_MODELS",
]


class UserModel(UserMixin, Base):
    """Tabla `darwin_user`. No hereda `BaseModel[T]`: ver el docstring de `models_mixins`."""

    __tablename__ = DEFAULT_USER_TABLE


class SessionModel(SessionMixin, Base):
    """Tabla `darwin_session`, con `actor_user_id` y `subject_user_id` separados."""

    __tablename__ = DEFAULT_SESSION_TABLE


class AccountModel(AccountMixin, Base):
    """Tabla `darwin_account`: OAuth y la credencial local."""

    __tablename__ = DEFAULT_ACCOUNT_TABLE


class VerificationModel(VerificationMixin, Base):
    """Tabla `darwin_verification`: tokens de un solo uso."""

    __tablename__ = DEFAULT_VERIFICATION_TABLE


class AuditLogModel(AuditLogMixin, Base):
    """Tabla `darwin_audit_log`."""

    __tablename__ = DEFAULT_AUDIT_TABLE


class JwksModel(JwksMixin, Base):
    """Tabla `darwin_jwks`: claves de firma y su estado de rotación."""

    __tablename__ = DEFAULT_JWKS_TABLE


#: Las 6 tablas por defecto, en orden de dependencia de FK — `user` primero, porque
#: `session` y `account` la referencian. `create_all` ordena solo, pero un `drop_all` o una
#: migración escrita a mano no, así que el orden explícito ahorra un error evitable.
#: `t.Any` y no `type`: `__table__` lo agrega el mapeo declarativo en tiempo de ejecución,
#: así que no existe en el tipo estático de una `type` cualquiera.
IDENTITY_MODELS: tuple[t.Any, ...] = (
    UserModel,
    SessionModel,
    AccountModel,
    VerificationModel,
    AuditLogModel,
    JwksModel,
)
