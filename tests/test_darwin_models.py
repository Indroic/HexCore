"""
Darwin Fase 3: las dos reglas de `cron_sql` y la separación mixin / modelo concreto.

Las dos reglas no son estilo: cada una evita un modo de falla concreto y caro.

1. **Los modelos no heredan `BaseModel[T]`.** `get_domain_entity()` devuelve
   `self._domain_entity` sin default, y el UoW lo llama para todo `BaseModel` trackeado. Una
   fila insertada sin `set_domain_entity()` explota en `commit()` **después** de que la
   transacción se confirmó, y nada rollbackea: fila escrita, 500 al usuario, en cada login.
2. **Los repositorios no heredan `BaseSQLAlchemyRepository`.** El discovery mapea
   `UserRepository` a la clave ``user`` y **levanta `ValueError`** ante una colisión, así que
   un repositorio de identidad autodescubrible rompería el UoW de todo consumidor que tenga
   el suyo.

Espeja `tests/test_cron_sql.py:66-90`, que fija las mismas reglas para la tabla de cron.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import String  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    AccountMixin,
    AccountModel,
    AuditLogModel,
    IDENTITY_MODELS,
    JwksModel,
    SessionMixin,
    SessionModel,
    UserMixin,
    UserModel,
    VerificationModel,
    identity_tables,
    validate_user_model,
)
from hexcore.darwin.infrastructure.repositories import (  # noqa: E402
    SqlAlchemyAccountRepository,
    SqlAlchemySessionRepository,
    SqlAlchemyUserRepository,
    SqlAlchemyVerificationRepository,
)
from hexcore.infrastructure.repositories.base import BaseSQLAlchemyRepository  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy import (  # noqa: E402
    Base,
    BaseModel,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

TODOS_LOS_MODELOS = (
    UserModel,
    SessionModel,
    AccountModel,
    VerificationModel,
    AuditLogModel,
    JwksModel,
)
TODOS_LOS_REPOS = (
    SqlAlchemyUserRepository,
    SqlAlchemySessionRepository,
    SqlAlchemyAccountRepository,
    SqlAlchemyVerificationRepository,
)


# ── Regla 1: no heredar BaseModel[T] ──────────────────────────────────────────
@pytest.mark.parametrize("modelo", TODOS_LOS_MODELOS, ids=lambda m: m.__name__)
def test_ningun_modelo_hereda_basemodel(modelo):
    """
    Si heredara, `collect_domain_entities()` le pediría una entidad de dominio y explotaría
    con `AttributeError` **después** del commit — dejando la fila escrita.
    """
    assert not issubclass(modelo, BaseModel)
    assert issubclass(modelo, Base)


def test_get_domain_entity_explotaria_de_verdad():
    """
    Documenta el mecanismo en vez de sólo afirmar la regla.

    Un `BaseModel` recién instanciado no tiene `_domain_entity`, así que `get_domain_entity()`
    lanza. Ese es exactamente el camino que recorrería el UoW.
    """

    class ModeloConDominio(BaseModel[object]):
        __tablename__ = "_prueba_dominio"

    try:
        with pytest.raises(AttributeError):
            ModeloConDominio().get_domain_entity()
    finally:
        Base.metadata.remove(ModeloConDominio.__table__)


# ── Regla 2: no ser autodescubrible ───────────────────────────────────────────
@pytest.mark.parametrize("repo", TODOS_LOS_REPOS, ids=lambda r: r.__name__)
def test_ningun_repositorio_hereda_la_base_de_dominio(repo):
    assert not issubclass(repo, BaseSQLAlchemyRepository)


@pytest.mark.parametrize("repo", TODOS_LOS_REPOS, ids=lambda r: r.__name__)
def test_ningun_repositorio_lo_levanta_el_auto_discovery(repo):
    from hexcore.infrastructure.repositories.utils import _get_all_subclasses

    assert repo not in _get_all_subclasses(BaseSQLAlchemyRepository)


def test_un_userrepository_de_la_app_no_colisiona():
    """
    **El test que prueba que no le rompimos el UoW a nadie.**

    `_repository_key_from_class_name` mapea `UserRepository` a la clave ``user``, y
    `_discover_repositories` levanta `ValueError` ante una colisión. Si
    `SqlAlchemyUserRepository` fuera autodescubrible, este escenario —que es el de
    prácticamente todo consumidor— reventaría al construir cualquier UoW.
    """
    from hexcore.infrastructure.repositories.utils import _discover_repositories

    class UserRepository(BaseSQLAlchemyRepository):
        """El repositorio de la app del consumidor."""

        async def get_by_id(self, entity_id):  # pragma: no cover
            ...

        async def list_all(self, limit=None, offset=0):  # pragma: no cover
            ...

        async def save(self, entity):  # pragma: no cover
            ...

        async def delete(self, entity):  # pragma: no cover
            ...

    # No levanta: es la prueba de que los repos de Darwin no compiten por la clave `user`.
    descubiertos = _discover_repositories(BaseSQLAlchemyRepository)

    assert descubiertos["user"] is UserRepository


# ── Mixins vs modelos concretos ───────────────────────────────────────────────
def test_importar_los_mixins_no_registra_tablas():
    """
    La propiedad central: permite que el consumidor declare las clases concretas en su propio
    paquete `models/`, donde `import_all_models` las ve y `--autogenerate` no las dropea.

    Se mide en un **subproceso** y no recargando el módulo: un `sys.modules.pop` acá crearía
    un `UserMixin` nuevo, y `UserModel` —ya construido a partir del viejo— dejaría de ser su
    subclase, rompiendo los otros tests de este archivo.
    """
    import subprocess
    import sys

    codigo = (
        "from hexcore.infrastructure.repositories.orms.sqlalchemy import Base;"
        "antes = set(Base.metadata.tables);"
        "import hexcore.darwin.infrastructure.models_mixins;"
        "print(sorted(set(Base.metadata.tables) - antes))"
    )
    salida = subprocess.run(
        [sys.executable, "-c", codigo],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert salida.returncode == 0, salida.stderr
    assert salida.stdout.strip() == "[]"


@pytest.mark.parametrize(
    "mixin",
    [UserMixin, SessionMixin, AccountMixin],
    ids=lambda m: m.__name__,
)
def test_los_mixins_no_estan_mapeados(mixin):
    """El mecanismo: no heredan `Base`, así que SQLAlchemy nunca los mapea."""
    assert not issubclass(mixin, Base)
    assert not hasattr(mixin, "__tablename__")
    assert not hasattr(mixin, "__table__")


def test_importar_los_modelos_registra_exactamente_seis():
    import hexcore.darwin.infrastructure.models  # noqa: F401

    esperadas = {
        "darwin_user",
        "darwin_session",
        "darwin_account",
        "darwin_verification",
        "darwin_audit_log",
        "darwin_jwks",
    }

    assert esperadas <= set(Base.metadata.tables)
    assert len(IDENTITY_MODELS) == 6
    assert len(identity_tables()) == 6


def test_se_puede_renombrar_la_tabla_via_mixin():
    """Sin esto, un consumidor con un esquema propio tendría que forkear."""

    class MiUsuario(UserMixin, Base):
        __tablename__ = "cuentas"

    try:
        assert MiUsuario.__table__.name == "cuentas"
        # Los constraints derivan del nombre real, no de uno fijo.
        nombres = {c.name for c in MiUsuario.__table__.constraints}
        assert "uq_cuentas_email" in nombres
        assert {i.name for i in MiUsuario.__table__.indexes} == {"ix_cuentas_created_at"}
    finally:
        Base.metadata.remove(MiUsuario.__table__)


def test_renombrar_la_tabla_de_usuarios_retargetea_las_fks():
    """
    `__darwin_user_table__` existe porque una FK no puede derivar sola a dónde apunta.

    Sin esto, renombrar `user` dejaría las FKs de `session` y `account` apuntando a una tabla
    inexistente, y el error aparecería recién al crear el esquema.
    """

    class MiUsuario(UserMixin, Base):
        __tablename__ = "cuentas"

    class MiSesion(SessionMixin, Base):
        __tablename__ = "cuentas_sesion"
        __darwin_user_table__ = "cuentas"

    class MiCuenta(AccountMixin, Base):
        __tablename__ = "cuentas_externas"
        __darwin_user_table__ = "cuentas"

    try:
        destinos = {
            fk.column.table.name for fk in MiSesion.__table__.foreign_keys
        }
        assert destinos == {"cuentas"}
        assert {fk.column.table.name for fk in MiCuenta.__table__.foreign_keys} == {
            "cuentas"
        }
    finally:
        for modelo in (MiCuenta, MiSesion, MiUsuario):
            Base.metadata.remove(modelo.__table__)


def test_el_consumidor_puede_agregar_columnas():
    """La estrategia híbrida: columnas propias sobre el mixin."""

    class UsuarioConPlan(UserMixin, Base):
        __tablename__ = "usuario_con_plan"
        plan: Mapped[str] = mapped_column(String(32), default="free")

    try:
        assert "plan" in UsuarioConPlan.__table__.columns
        assert "email" in UsuarioConPlan.__table__.columns
    finally:
        Base.metadata.remove(UsuarioConPlan.__table__)


# ── Esquema ───────────────────────────────────────────────────────────────────
def test_la_sesion_tiene_dos_principales_y_ningun_user_id():
    """El desvío más importante frente a Better Auth."""
    columnas = SessionModel.__table__.columns

    assert "actor_user_id" in columnas
    assert "subject_user_id" in columnas
    assert "user_id" not in columnas
    assert columnas["actor_user_id"].nullable is False
    assert columnas["subject_user_id"].nullable is False


def test_los_constraints_criticos_existen_con_nombre():
    """
    Los nombres vienen de la `naming_convention` agregada en el prerrequisito P2: sin ella,
    SQLite no puede dropearlos y difieren entre backends.
    """
    unicos_usuario = {
        c.name for c in UserModel.__table__.constraints if c.name is not None
    }
    assert "uq_darwin_user_email" in unicos_usuario

    unicos_cuenta = {
        c.name for c in AccountModel.__table__.constraints if c.name is not None
    }
    # El constraint que hace segura la vinculación OAuth.
    assert "uq_darwin_account_provider_account" in unicos_cuenta

    unicos_sesion = {
        c.name for c in SessionModel.__table__.constraints if c.name is not None
    }
    assert "uq_darwin_session_token_hash" in unicos_sesion


def test_no_se_guarda_ningun_secreto_en_claro():
    """Un dump de estas tablas no puede ser un set de credenciales utilizables."""
    assert "token_hash" in SessionModel.__table__.columns
    assert "token" not in SessionModel.__table__.columns

    assert "value_hash" in VerificationModel.__table__.columns
    assert "value" not in VerificationModel.__table__.columns


def test_la_auditoria_no_tiene_fk_al_usuario():
    """
    Dos motivos: un principal de sistema no es una fila de `user`, y una FK con CASCADE
    borraría el registro al borrar el usuario — lo contrario de lo que una auditoría hace.
    """
    assert AuditLogModel.__table__.foreign_keys == set()
    assert "actor_id" in AuditLogModel.__table__.columns


def test_la_columna_de_metadatos_no_se_llama_metadata():
    """`Base.metadata` es el `MetaData` de SQLAlchemy: una columna así lo pisaría."""
    assert "audit_metadata" in AuditLogModel.__table__.columns
    assert "metadata" not in AuditLogModel.__table__.columns


# ── validate_user_model ───────────────────────────────────────────────────────
def test_validate_rechaza_un_basemodel():
    class UsuarioMal(BaseModel[object]):
        __tablename__ = "_usuario_mal"

    try:
        with pytest.raises(TypeError, match="BaseModel"):
            validate_user_model(UsuarioMal)
    finally:
        Base.metadata.remove(UsuarioMal.__table__)


def test_validate_rechaza_una_clase_sin_el_mixin():
    class NoEsUsuario:
        pass

    with pytest.raises(TypeError, match="UserMixin"):
        validate_user_model(NoEsUsuario)


def test_validate_acepta_el_modelo_por_defecto_y_uno_extendido():
    validate_user_model(UserModel)

    class UsuarioExtendido(UserMixin, Base):
        __tablename__ = "usuario_extendido"
        plan: Mapped[str] = mapped_column(String(32), default="free")

    try:
        validate_user_model(UsuarioExtendido)
    finally:
        Base.metadata.remove(UsuarioExtendido.__table__)


def test_el_error_de_validacion_trae_el_reemplazo_copiable():
    class UsuarioMal(BaseModel[object]):
        __tablename__ = "_usuario_mal_2"

    try:
        with pytest.raises(TypeError) as excinfo:
            validate_user_model(UsuarioMal)
        mensaje = str(excinfo.value)
        assert "from hexcore.darwin import UserMixin" in mensaje
        assert "class UsuarioMal(UserMixin, Base):" in mensaje
    finally:
        Base.metadata.remove(UsuarioMal.__table__)
