"""
Darwin — Fase F5 del plan rbac/drbac: `DrbacService`, los repositorios SQL y el cableado del
plugin.

Lo que se fija:

1. CRUD de políticas y bindings contra SQLite real, con `Policy`/`RoleBinding` embebiendo lo
   que corresponda (reglas de una política) tal como se guardaron.
2. `enforce_condition_limits` corre **al guardar**, no sólo al evaluar.
3. `client_evaluable` nunca queda `True` si la condición usa `Predicate`, sin importar lo que
   declaró quien creó la regla.
4. Editar o borrar una política sube la versión de su scope; crear o revocar un binding **no**
   —no hay ninguna entrada cacheada que eso invalide, ver el docstring de `DrbacService`.
5. `DrbacPlugin.requires = ("rbac",)`: registrarlo solo, sin `RbacPlugin`, falla al validar.
6. El cableado completo: RBAC concede un permiso, DRBAC lo restringe condicionalmente, y
   `AuthorizationEngine` combina los dos con deny-overrides — la prueba de que las dos fases
   conviven en el mismo motor sin que ninguna sepa de la otra.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    FixedClock,
    IdentityConfig,
    PluginRegistry,
    StaticKeyStore,
    User,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    reset_identity,
)
from hexcore.darwin.domain.authorization import AccessRequest, ResourceRef  # noqa: E402
from hexcore.darwin.domain.context import AuthContext, Principal  # noqa: E402
from hexcore.darwin.infrastructure.orms.sqlalchemy.repositories import (  # noqa: E402
    SqlAlchemyUserRepository,
)
from hexcore.darwin.plugins.drbac import DrbacPlugin, get_drbac_service  # noqa: E402
from hexcore.darwin.plugins.drbac.conditions import (  # noqa: E402
    ConditionTooComplexError,
    Eq,
    In,
    Var,
)
from hexcore.darwin.plugins.drbac.domain import (  # noqa: E402
    DRBAC_EXCEPTION_STATUS_MAP,
    PolicyAlreadyExistsError,
    PolicyNotFoundError,
    RoleBindingNotFoundError,
)
from hexcore.darwin.plugins.drbac.orms.sqlalchemy.repository import (  # noqa: E402
    AuthzVersionRepository,
    PolicyRepository,
    RoleBindingRepository,
)
from hexcore.darwin.plugins.drbac.service import DrbacService  # noqa: E402
from hexcore.darwin.plugins.rbac import RbacPlugin, get_rbac_service  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def engine():
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor, plugins=["rbac", "drbac"]))
    yield motor
    asyncio.run(dispose_engine())


@pytest.fixture
def servicio(engine) -> DrbacService:
    return DrbacService(
        policies=PolicyRepository(),
        bindings=RoleBindingRepository(),
        versions=AuthzVersionRepository(),
        clock=FixedClock(AHORA),
    )


async def _usuario(email: str = "ana@ejemplo.com") -> User:
    return await SqlAlchemyUserRepository().add(User(email=email))


# ── CRUD de políticas ────────────────────────────────────────────────────────────
class TestCRUDDePoliticas:
    @pytest.mark.anyio
    async def test_crear_y_leer(self, servicio: DrbacService):
        politica = await servicio.create_policy(
            scope_key="org:1",
            name="no-self-approval",
            rules=[
                {
                    "effect": "deny",
                    "actions": ["invoice.approve"],
                    "resource_type": "invoice",
                    "condition": Eq(Var("resource.owner_id"), Var("subject.id")),
                }
            ],
        )
        leida = await servicio.get_policy(politica.id)

        assert leida.name == "no-self-approval"
        assert leida.scope_key == "org:1"
        assert len(leida.rules) == 1
        assert leida.rules[0].effect == "deny"
        assert leida.rules[0].condition == Eq(Var("resource.owner_id"), Var("subject.id"))

    @pytest.mark.anyio
    async def test_nombre_duplicado_en_el_mismo_scope_lanza(self, servicio: DrbacService):
        await servicio.create_policy(scope_key="org:1", name="p")
        with pytest.raises(PolicyAlreadyExistsError):
            await servicio.create_policy(scope_key="org:1", name="p")

    @pytest.mark.anyio
    async def test_mismo_nombre_en_otro_scope_no_lanza(self, servicio: DrbacService):
        await servicio.create_policy(scope_key="org:1", name="p")
        # no debería lanzar
        await servicio.create_policy(scope_key="org:2", name="p")

    @pytest.mark.anyio
    async def test_get_policy_inexistente_lanza(self, servicio: DrbacService):
        with pytest.raises(PolicyNotFoundError):
            await servicio.get_policy(uuid4())

    @pytest.mark.anyio
    async def test_update_reemplaza_las_reglas_enteras(self, servicio: DrbacService):
        politica = await servicio.create_policy(
            scope_key="org:1",
            name="p",
            rules=[{"effect": "allow", "actions": ["a.b"], "resource_type": "a"}],
        )
        actualizada = await servicio.update_policy(
            politica.id,
            rules=[{"effect": "deny", "actions": ["c.d"], "resource_type": "c"}],
        )
        assert len(actualizada.rules) == 1
        assert actualizada.rules[0].actions == ("c.d",)

    @pytest.mark.anyio
    async def test_update_sin_rules_conserva_las_existentes(self, servicio: DrbacService):
        politica = await servicio.create_policy(
            scope_key="org:1",
            name="p",
            rules=[{"effect": "allow", "actions": ["a.b"], "resource_type": "a"}],
        )
        actualizada = await servicio.update_policy(politica.id, description="nueva")
        assert actualizada.description == "nueva"
        assert len(actualizada.rules) == 1

    @pytest.mark.anyio
    async def test_delete_policy(self, servicio: DrbacService):
        politica = await servicio.create_policy(scope_key="org:1", name="p")
        assert await servicio.delete_policy(politica.id) is True
        with pytest.raises(PolicyNotFoundError):
            await servicio.get_policy(politica.id)

    @pytest.mark.anyio
    async def test_list_policies_ordena_por_prioridad(self, servicio: DrbacService):
        await servicio.create_policy(scope_key="org:1", name="baja", priority=200)
        await servicio.create_policy(scope_key="org:1", name="alta", priority=10)
        listado = await servicio.list_policies("org:1")
        assert [p.name for p in listado] == ["alta", "baja"]


# ── Límites y validación al guardar ────────────────────────────────────────────
class TestValidacionAlGuardar:
    @pytest.mark.anyio
    async def test_condicion_demasiado_grande_lanza_al_crear(self, servicio: DrbacService):
        from hexcore.darwin.plugins.drbac.conditions import And, Const

        enorme = And(*[Eq(Const(1), Const(1)) for _ in range(300)])
        with pytest.raises(ConditionTooComplexError):
            await servicio.create_policy(
                scope_key="org:1",
                name="p",
                rules=[
                    {
                        "effect": "allow",
                        "actions": ["a.b"],
                        "resource_type": "a",
                        "condition": enorme,
                    }
                ],
            )

    @pytest.mark.anyio
    async def test_client_evaluable_se_fuerza_a_false_con_predicate(
        self, servicio: DrbacService
    ):
        from hexcore.darwin.plugins.drbac.conditions import And, Predicate

        politica = await servicio.create_policy(
            scope_key="org:1",
            name="p",
            rules=[
                {
                    "effect": "allow",
                    "actions": ["a.b"],
                    "resource_type": "a",
                    "condition": And(Eq(Var("subject.id"), Var("subject.id")), Predicate("x")),
                    "client_evaluable": True,
                }
            ],
        )
        assert politica.rules[0].client_evaluable is False

    @pytest.mark.anyio
    async def test_client_evaluable_se_respeta_sin_predicate(self, servicio: DrbacService):
        politica = await servicio.create_policy(
            scope_key="org:1",
            name="p",
            rules=[
                {
                    "effect": "allow",
                    "actions": ["a.b"],
                    "resource_type": "a",
                    "condition": Eq(Var("subject.id"), Var("subject.id")),
                    "client_evaluable": True,
                }
            ],
        )
        assert politica.rules[0].client_evaluable is True


# ── Versión de scope ─────────────────────────────────────────────────────────────
class TestVersion:
    @pytest.mark.anyio
    async def test_crear_politica_sube_la_version(self, servicio: DrbacService):
        v0 = await servicio.authz_version("org:1")
        await servicio.create_policy(scope_key="org:1", name="p")
        v1 = await servicio.authz_version("org:1")
        assert v1 > v0

    @pytest.mark.anyio
    async def test_actualizar_y_borrar_politica_suben_la_version(self, servicio: DrbacService):
        politica = await servicio.create_policy(scope_key="org:1", name="p")
        v1 = await servicio.authz_version("org:1")
        await servicio.update_policy(politica.id, enabled=False)
        v2 = await servicio.authz_version("org:1")
        assert v2 > v1
        await servicio.delete_policy(politica.id)
        v3 = await servicio.authz_version("org:1")
        assert v3 > v2

    @pytest.mark.anyio
    async def test_crear_o_revocar_un_binding_no_sube_la_version(self, servicio: DrbacService):
        """Ver el docstring de `DrbacService`: los bindings se leen siempre en vivo, sin
        cache, así que no hay nada que invalidar."""
        usuario = await _usuario()
        v0 = await servicio.authz_version("org:1")
        binding = await servicio.create_binding(
            subject_id=usuario.id, role_name="accountant", scope_path="org:1"
        )
        v1 = await servicio.authz_version("org:1")
        assert v1 == v0
        await servicio.revoke_binding(binding.id)
        v2 = await servicio.authz_version("org:1")
        assert v2 == v0


# ── Bindings ───────────────────────────────────────────────────────────────────
class TestBindings:
    @pytest.mark.anyio
    async def test_crear_listar_y_revocar(self, servicio: DrbacService):
        usuario = await _usuario()
        binding = await servicio.create_binding(
            subject_id=usuario.id, role_name="accountant", scope_path="org:1"
        )
        listado = await servicio.list_bindings_for_subject(usuario.id)
        assert [b.id for b in listado] == [binding.id]

        assert await servicio.revoke_binding(binding.id) is True
        assert await servicio.list_bindings_for_subject(usuario.id) == []

    @pytest.mark.anyio
    async def test_revocar_inexistente_lanza(self, servicio: DrbacService):
        with pytest.raises(RoleBindingNotFoundError):
            await servicio.revoke_binding(uuid4())

    @pytest.mark.anyio
    async def test_binding_con_condicion_demasiado_grande_lanza(self, servicio: DrbacService):
        from hexcore.darwin.plugins.drbac.conditions import And, Const

        usuario = await _usuario()
        enorme = And(*[Eq(Const(1), Const(1)) for _ in range(300)])
        with pytest.raises(ConditionTooComplexError):
            await servicio.create_binding(
                subject_id=usuario.id,
                role_name="accountant",
                scope_path="org:1",
                condition=enorme,
            )

    @pytest.mark.anyio
    async def test_active_for_subject_at_scopes_respeta_vencimiento(
        self, servicio: DrbacService
    ):
        usuario = await _usuario()
        await servicio.create_binding(
            subject_id=usuario.id,
            role_name="temp",
            scope_path="org:1",
            expires_at=AHORA + timedelta(minutes=1),
            at=AHORA,
        )
        vigentes = await servicio._bindings.active_for_subject_at_scopes(
            usuario.id, ["org:1"], at=AHORA
        )
        vencidos = await servicio._bindings.active_for_subject_at_scopes(
            usuario.id, ["org:1"], at=AHORA + timedelta(minutes=5)
        )
        assert len(vigentes) == 1
        assert len(vencidos) == 0


# ── El plugin ──────────────────────────────────────────────────────────────────
class TestDrbacPlugin:
    def test_contributed_tables_coincide_con_tables(self):
        plugin = DrbacPlugin()
        assert tuple(DrbacPlugin.contributed_tables) == tuple(plugin.tables())

    def test_exception_status_map(self):
        plugin = DrbacPlugin()
        assert plugin.exception_status_map() == DRBAC_EXCEPTION_STATUS_MAP

    def test_incluye_un_router(self):
        assert len(DrbacPlugin().routers()) == 1

    def test_sin_router_si_include_router_es_false(self):
        assert DrbacPlugin(include_router=False).routers() == ()

    def test_requiere_rbac(self):
        assert DrbacPlugin.requires == ("rbac",)

    def test_registrar_drbac_sin_rbac_falla_al_validar(self, engine):
        reset_identity()
        try:
            with pytest.raises(Exception, match="requires"):
                configure_identity(
                    IdentityConfig(storage="sqlalchemy", secret_key=CLAVE),
                    plugins=PluginRegistry([DrbacPlugin()]),
                    clock=FixedClock(AHORA),
                    key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
                )
        finally:
            reset_identity()

    def test_incluye_un_provider_de_autorizacion(self, engine):
        reset_identity()
        try:
            drbac = DrbacPlugin()
            configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key=CLAVE),
                plugins=PluginRegistry([RbacPlugin(), drbac]),
                clock=FixedClock(AHORA),
                key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            )
            proveedores = drbac.authorization_providers()
            assert len(proveedores) == 1
            assert proveedores[0].name == "drbac"
        finally:
            reset_identity()


# ── El cableado completo: RBAC concede, DRBAC restringe ────────────────────────
class TestCableadoCompleto:
    @pytest.mark.anyio
    async def test_drbac_deniega_lo_que_rbac_concedio(self, engine):
        """
        La prueba central de F5: RBAC le da `invoice.approve` a todo `accountant`, DRBAC agrega
        "salvo la tuya propia" en el mismo scope, y `AuthorizationEngine` combina las dos
        decisiones con deny-overrides — sin que ninguno de los dos plugins importe al otro.
        """
        reset_identity()
        try:
            rbac_plugin = RbacPlugin()
            drbac_plugin = DrbacPlugin()
            configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key=CLAVE),
                plugins=PluginRegistry([rbac_plugin, drbac_plugin]),
                clock=FixedClock(AHORA),
                key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            )
            for paso in rbac_plugin.startup_steps():
                await paso.start()

            rbac_servicio = get_rbac_service()
            drbac_servicio = get_drbac_service()

            admin = await _usuario("admin@test.com")
            rol = await rbac_servicio.create_role(name="accountant")
            # Bootstrap del primer rol: `set_role_permissions` siempre exige anti-escalada
            # (a diferencia de `assign_role`, que la saltea con `actor_id=None`), así que en
            # un despliegue nuevo el catálogo/los permisos directos se cargan por el
            # repositorio — mismo patrón que el `_admin()` de `test_darwin_rbac_service.py`.
            await rbac_servicio._permissions.ensure(["invoice.*"])
            await rbac_servicio._roles.set_permissions(rol.id, ["invoice.*"])
            await rbac_servicio.assign_role(
                actor_id=None, user_id=admin.id, role_id=rol.id, scope_key="org:1", at=AHORA
            )

            await drbac_servicio.create_policy(
                scope_key="org:1",
                name="no-self-approval",
                rules=[
                    {
                        "effect": "deny",
                        "actions": ["invoice.approve"],
                        "resource_type": "invoice",
                        "condition": Eq(Var("resource.owner_id"), Var("subject.id")),
                    }
                ],
            )

            from hexcore.darwin.application.container import get_identity_container

            authorizer = get_identity_container().authorizer()
            actor = Principal(user_id=admin.id, scopes=frozenset())
            contexto = AuthContext(actor=actor, subject=actor, transport="bearer")

            # Aprobar la factura de otra persona: RBAC concede, DRBAC no aplica.
            otro_id = uuid4()
            decision_ajena = await authorizer.decide(
                AccessRequest(
                    context=contexto,
                    action="invoice.approve",
                    resource=ResourceRef(
                        type="invoice", id="1", owner_id=otro_id, scope_path="org:1"
                    ),
                )
            )
            assert decision_ajena.allowed is True

            # Aprobar la propia: RBAC concede, DRBAC deniega, y deny-overrides gana.
            decision_propia = await authorizer.decide(
                AccessRequest(
                    context=contexto,
                    action="invoice.approve",
                    resource=ResourceRef(
                        type="invoice", id="2", owner_id=admin.id, scope_path="org:1"
                    ),
                )
            )
            assert decision_propia.allowed is False
        finally:
            reset_identity()
