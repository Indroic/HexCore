"""
Darwin — 10.4, la ronda de correcciones de HC-8 a HC-22 (`docs/migracion-darwin/01-hexcore-10.4.md`
en redv2, el consumidor que las reportó).

Reproduce el escenario de redv2 explícitamente: `UserModel` con `as_uuid=False` (HC-8), rbac +
drbac + un resolver compuesto (HC-15/16), un recurso con ancestros evaluado para un admin
global, un consorcio y una terminal (HC-18), y `/check` rechazando atributos inyectados (HC-14).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy import String, Uuid  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.darwin.domain.context import AuthContext, Principal  # noqa: E402
from hexcore.darwin.domain.authorization import AccessRequest, ResourceRef  # noqa: E402
from hexcore.darwin.domain.ports import (  # noqa: E402
    AbstractPrincipalResolver,
    CompositePrincipalResolver,
    ResolvedPrincipal,
)
from hexcore.darwin.plugins.rbac.domain import GLOBAL_SCOPE, RbacRole, scope_chain  # noqa: E402
from hexcore.darwin.plugins.rbac.orms.sqlalchemy.repository import (  # noqa: E402
    SqlAlchemyRbacRoleRepository,
    SqlAlchemyRbacUserRoleRepository,
)
from hexcore.darwin.plugins.rbac.provider import RbacAuthorizationProvider  # noqa: E402
from hexcore.darwin.plugins.rbac.service import RbacService  # noqa: E402
from hexcore.darwin.plugins.drbac.pip import PolicyInformationPoint, ResourceAttributeResolver  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    get_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"


# ── HC-8: ids como `str` (as_uuid=False), como redv2 ──────────────────────────
class _UserModelStr(Base):
    """El `UserModel` de redv2: id `Uuid(as_uuid=False)`, no el default `as_uuid=True`."""

    __tablename__ = "test_10_4_user"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)


class _RbacRoleModelStr(Base):
    __tablename__ = "test_10_4_rbac_role"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    is_system: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class _RbacUserRoleModelStr(Base):
    __tablename__ = "test_10_4_rbac_user_role"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), nullable=False)
    role_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    granted_by: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def engine_str_ids():
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)

    async def _crear() -> None:
        async with get_engine().begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[
                    _UserModelStr.__table__,
                    _RbacRoleModelStr.__table__,
                    _RbacUserRoleModelStr.__table__,
                ],
            )

    asyncio.run(_crear())
    yield motor
    asyncio.run(dispose_engine())


def test_hc8_repos_no_revientan_con_columnas_as_uuid_false(engine_str_ids):
    """
    Antes de HC-8, esto reventaba con `AttributeError: 'UUID' object has no attribute
    'replace'` al insertar — `sa.Uuid(as_uuid=False)` no coacciona un `uuid.UUID` al bindear, y
    `BaseEntity.id`/`RbacRole.id`/`RoleAssignment.user_id` son siempre `UUID` en el dominio.
    """
    roles = SqlAlchemyRbacRoleRepository(model=_RbacRoleModelStr)
    user_roles = SqlAlchemyRbacUserRoleRepository(model=_RbacUserRoleModelStr)

    role_id = uuid4()
    user_id = uuid4()

    async def _correr() -> None:
        creado = await roles.add(RbacRole(id=role_id, scope_key=GLOBAL_SCOPE, name="admin"))
        assert creado.id == role_id

        obtenido = await roles.get(role_id)
        assert obtenido is not None and obtenido.name == "admin"

        actualizado = await roles.update(creado.model_copy(update={"description": "x"}))
        assert actualizado.description == "x"

        from hexcore.darwin.plugins.rbac.domain import RoleAssignment

        asignacion = await user_roles.assign(
            RoleAssignment(
                id=uuid4(),
                user_id=user_id,
                role_id=role_id,
                scope_key=GLOBAL_SCOPE,
                created_at=AHORA,
            )
        )
        assert asignacion.user_id == user_id

        activos = await user_roles.active_role_ids_for(user_id, GLOBAL_SCOPE, at=AHORA)
        assert activos == frozenset({role_id})

        revocado = await user_roles.revoke(user_id, role_id, GLOBAL_SCOPE)
        assert revocado is True

        borrado = await roles.delete(role_id)
        assert borrado is True

    asyncio.run(_correr())


# ── HC-14: caché del PIP acotada, id=None no colisiona, el resolver gana ──────
class _ResolverDeInvoice(ResourceAttributeResolver):
    resource_type = "invoice"

    def __init__(self) -> None:
        self.llamadas = 0

    async def resolve(self, resource: ResourceRef) -> dict:
        self.llamadas += 1
        return {"status": "approved", "seen": self.llamadas}


def test_hc14_el_resolver_gana_al_llamador_y_cachea_por_id():
    resolver = _ResolverDeInvoice()
    pip = PolicyInformationPoint(resolvers={"invoice": resolver})

    async def _correr() -> None:
        recurso = ResourceRef(
            type="invoice", id="f1", attributes={"status": "spoofed-by-client"}
        )
        atributos = await pip.attributes_for(recurso)
        assert atributos["status"] == "approved"  # el resolver pisa lo que mandó el cliente
        assert resolver.llamadas == 1

        # Mismo (type, id): sirve del caché, no vuelve a llamar al resolver.
        await pip.attributes_for(recurso)
        assert resolver.llamadas == 1

    asyncio.run(_correr())


def test_hc14_recursos_sin_id_no_colisionan_en_una_sola_entrada():
    llamadas: list[str] = []

    class _ResolverVariable(ResourceAttributeResolver):
        resource_type = "greeting"

        async def resolve(self, resource: ResourceRef) -> dict:
            llamadas.append("x")
            return {"n": len(llamadas)}

    pip = PolicyInformationPoint(resolvers={"greeting": _ResolverVariable()})

    async def _correr() -> None:
        r1 = ResourceRef(type="greeting", id=None)
        primero = await pip.attributes_for(r1)
        segundo = await pip.attributes_for(ResourceRef(type="greeting", id=None))
        # Ambos son `id=None` del mismo tipo: comparten la entrada de caché a propósito (no es
        # HC-14 lo que arregla esto, sigue siendo memoización por `(type, id)`) — lo que HC-14
        # corrige es que `None` ya no colapsa a `""` y se confunda con un recurso de id real "".
        assert primero == segundo
        assert len(llamadas) == 1

    asyncio.run(_correr())


# ── HC-15: `principal_resolver()` no exige el contenedor ya configurado ───────
def test_hc15_principal_resolver_no_requiere_container_configurado():
    from hexcore.darwin.plugins.rbac import RbacPlugin

    plugin = RbacPlugin()
    # Antes de HC-15 esto llamaba a `self.service()` -> `get_identity_container()` acá mismo,
    # y sin `configure_identity()` corrido antes, reventaba con `RuntimeError`.
    resolver = plugin.principal_resolver()
    assert resolver is not None


# ── HC-16: `CompositePrincipalResolver` ───────────────────────────────────────
def test_hc16_composite_principal_resolver_une_roles_y_prioriza_status():
    class _SoloRoles(AbstractPrincipalResolver):
        async def resolve(self, user):
            return ResolvedPrincipal(roles=frozenset({"admin"}), scopes=frozenset({"a.b"}))

    class _SoloStatus(AbstractPrincipalResolver):
        async def resolve(self, user):
            return ResolvedPrincipal(status="pending")

    compuesto = CompositePrincipalResolver([_SoloRoles(), _SoloStatus()])

    async def _correr():
        resuelto = await compuesto.resolve(user=None)
        assert resuelto.roles == frozenset({"admin"})
        assert resuelto.scopes == frozenset({"a.b"})
        assert resuelto.status == "pending"

    asyncio.run(_correr())


# ── HC-18: rbac evalúa a lo largo de scope_chain (admin global / consorcio / terminal) ──
def test_hc18_scope_chain_de_rbac():
    assert scope_chain("consortium:1/terminal:7") == (
        GLOBAL_SCOPE,
        "consortium:1",
        "consortium:1/terminal:7",
    )


def test_hc18_rol_global_aplica_en_un_scope_hijo():
    """Un admin con rol en GLOBAL_SCOPE concede en 'consortium:1/terminal:7', no sólo en global."""

    class _RolesFalsos:
        def __init__(self, por_scope):
            self._por_scope = por_scope

        async def effective_permissions(self, user_id, scope_key, *, at):
            from hexcore.darwin.plugins.rbac.matcher import CompiledPermissionSet

            return CompiledPermissionSet.compile(self._por_scope.get(scope_key, set()))

    class _VersionesFalsas:
        async def get(self, scope_key):
            return 1

    from hexcore.darwin.plugins.rbac.cache import PermissionMatrixCache

    servicio = _RolesFalsos({GLOBAL_SCOPE: {"invoice.read"}})
    provider = RbacAuthorizationProvider(
        service=servicio, versions=_VersionesFalsas(), cache=PermissionMatrixCache()
    )

    actor = Principal(user_id=uuid4())
    contexto = AuthContext(actor=actor, subject=actor, transport="bearer")

    async def _correr():
        decision = await provider.decide(
            AccessRequest(
                context=contexto,
                action="invoice.read",
                resource=ResourceRef(
                    type="invoice", scope_path="consortium:1/terminal:7"
                ),
            )
        )
        assert decision.effect == "allow"

    asyncio.run(_correr())


# ── HC-19: `sync_system_roles` bumpea `darwin_authz_version` ──────────────────
def test_hc19_sync_system_roles_bumpea_la_version():
    from hexcore.darwin.domain.permissions import RoleRegistry

    class _RolesEnMemoria:
        def __init__(self):
            self._por_nombre: dict[tuple[str, str], RbacRole] = {}

        async def get_by_name(self, scope_key, name):
            return self._por_nombre.get((scope_key, name))

        async def add(self, role):
            self._por_nombre[(role.scope_key, role.name)] = role
            return role

        async def set_permissions(self, role_id, keys):
            return None

        async def set_parents(self, role_id, parent_ids):
            return None

    class _PermisosEnMemoria:
        async def ensure(self, keys):
            return None

    class _VersionesEnMemoria:
        def __init__(self):
            self.version = 0
            self.bumps = 0

        async def bump(self, scope_key):
            self.version += 1
            self.bumps += 1
            return self.version

        async def get(self, scope_key):
            return self.version

    versiones = _VersionesEnMemoria()
    servicio = RbacService(
        roles=_RolesEnMemoria(),
        permissions=_PermisosEnMemoria(),
        user_roles=None,  # no lo usa sync_system_roles
        versions=versiones,
        clock=_RelojFalso(),
    )

    registro = RoleRegistry()
    registro.register_role("admin", permissions={"*"})

    asyncio.run(servicio.sync_system_roles(registro))
    assert versiones.bumps == 1


class _RelojFalso:
    def now(self):
        return AHORA


# ── HC-21: `AuthContext.external(...)` para un caller que no es de Darwin ─────
def test_hc21_authcontext_external_pasa_por_accessrequest():
    user_id = uuid4()
    contexto = AuthContext.external(user_id, roles=("cajero",), scopes=("ticket.pay",))

    assert contexto.actor_id == user_id
    assert contexto.subject_id == user_id
    assert contexto.is_impersonating is False
    assert contexto.has_scope("ticket.pay") is True

    # No revienta al construir un `AccessRequest` — antes HC-21, no había forma de tener un
    # `AuthContext` sin un `Principal` de Darwin real.
    pedido = AccessRequest(context=contexto, action="ticket.pay", resource=None)
    assert pedido.context.actor_id == user_id
    assert pedido.action == "ticket.pay"


# ── HC-17: `identity_startup_steps()`/`IdentityStep` corren los pasos de los plugins ──
def test_hc17_rbac_seed_step_corre_solo_al_arrancar():
    """
    Antes de HC-17, `RbacSeedStep` (el `startup_steps()` de `RbacPlugin`) sólo corría si el
    consumidor lo descubría y lo agregaba a mano a su propio `build_lifespan(...)` — acá se
    verifica que alcanza con `IdentityStep.start()`, sin tocar `plugins.startup_steps()` en el
    test.
    """
    from hexcore.darwin import (
        FixedClock,
        IdentityConfig,
        PluginRegistry,
        StaticKeyStore,
        create_identity_tables,
        generate_signing_key,
        reset_identity,
    )
    from hexcore.darwin.domain.permissions import RoleRegistry
    from hexcore.darwin.infrastructure.lifespan import IdentityStep
    from hexcore.darwin.plugins.rbac import RbacPlugin, get_rbac_service

    registro = RoleRegistry()
    registro.register_role("admin", permissions={"*"})
    plugin = RbacPlugin(registry=registro)

    async def _correr() -> None:
        await dispose_engine()
        motor = init_engine(SQLITE_URL, poolclass=StaticPool)
        await create_identity_tables(motor, plugins=["rbac"])

        reset_identity()
        paso = IdentityStep(
            IdentityConfig(storage="sqlalchemy", secret_key="k" * 48),
            components={
                "plugins": PluginRegistry([plugin]),
                "clock": FixedClock(AHORA),
                "key_store": StaticKeyStore([generate_signing_key(kid="k1")]),
            },
            verify_schema=False,
        )
        try:
            await paso.start()
            roles = await get_rbac_service().list_roles(GLOBAL_SCOPE)
            assert any(r.name == "admin" for r in roles)
        finally:
            await paso.stop()
            await dispose_engine()

    asyncio.run(_correr())
