"""
Los modelos concretos de `drbac`. **Importar este módulo SÍ registra las cuatro tablas.**

Contraparte deliberada de `models_mixins`, igual que en el núcleo y en `rbac`.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.plugins.drbac.orms.sqlalchemy.models_mixins import (
    DEFAULT_AUTHZ_VERSION_TABLE,
    DEFAULT_POLICY_TABLE,
    DEFAULT_ROLE_BINDING_TABLE,
    DEFAULT_RULE_TABLE,
    DrbacAuthzVersionMixin,
    DrbacPolicyMixin,
    DrbacRoleBindingMixin,
    DrbacRuleMixin,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

__all__ = [
    "PLUGIN_MODELS",
    "DrbacPolicyModel",
    "DrbacRuleModel",
    "DrbacRoleBindingModel",
    "DrbacAuthzVersionModel",
    "DRBAC_MODELS",
    "create_drbac_tables",
]


class DrbacPolicyModel(DrbacPolicyMixin, Base):
    """Tabla `darwin_drbac_policy`."""

    __tablename__ = DEFAULT_POLICY_TABLE


class DrbacRuleModel(DrbacRuleMixin, Base):
    """Tabla `darwin_drbac_rule`."""

    __tablename__ = DEFAULT_RULE_TABLE


class DrbacRoleBindingModel(DrbacRoleBindingMixin, Base):
    """Tabla `darwin_drbac_role_binding`."""

    __tablename__ = DEFAULT_ROLE_BINDING_TABLE


class DrbacAuthzVersionModel(DrbacAuthzVersionMixin, Base):
    """Tabla `darwin_drbac_authz_version`."""

    __tablename__ = DEFAULT_AUTHZ_VERSION_TABLE


#: En orden de creación: `rule` y `role_binding` referencian a `policy`/`user` por FK.
DRBAC_MODELS = (
    DrbacPolicyModel,
    DrbacRuleModel,
    DrbacRoleBindingModel,
    DrbacAuthzVersionModel,
)


async def create_drbac_tables(
    engine: t.Any = None, *, models: t.Sequence[type] | None = None
) -> None:
    """Crea las tablas del plugin si no existen. Idempotente. **En producción usá Alembic.**"""
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    objetivo: t.Sequence[t.Any] = models if models is not None else DRBAC_MODELS
    tablas: list[t.Any] = [modelo.__table__ for modelo in objetivo]

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tablas)


# ── El contrato de esquema ───────────────────────────────────────────
PLUGIN_MODELS = DRBAC_MODELS
