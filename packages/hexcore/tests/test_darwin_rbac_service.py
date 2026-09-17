"""
Darwin — Fase F2 del plan rbac/drbac: `RbacService`, el resolver y el provider.

Lo que se fija, y por qué cada cosa importa:

1. **Anti-escalada.** Ni otorgando permisos directos a un rol, ni poniéndole un padre nuevo,
   ni asignando el rol a alguien, el actor puede terminar concediendo más de lo que él mismo
   tiene efectivamente en ese scope.
2. **Ciclos de herencia.** `set_role_parents` los rechaza antes de tocar la tabla.
3. **`sync_system_roles`** deja la tabla en el mismo estado que el `RoleRegistry`, y correrlo
   dos veces no duplica nada.
4. **`RbacAuthorizationProvider.decide()`** concede lo que el rol da y nada más, y dejar de
   tener el rol se nota en la decisión siguiente sin esperar caché.
5. **El wiring del plugin**: `contributed_tables` coincide con `tables()`, expone su mapa de
   excepciones, y `RbacSeedStep` deja los roles de código sincronizados.
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
from hexcore.darwin.domain.permissions import RoleRegistry  # noqa: E402
from hexcore.darwin.infrastructure.orms.sqlalchemy.repositories import (  # noqa: E402
    SqlAlchemyUserRepository,
)
from hexcore.darwin.plugins.rbac import RbacPlugin, get_rbac_service  # noqa: E402
from hexcore.darwin.plugins.rbac.domain import (  # noqa: E402
    EscalationError,
    RoleCycleError,
    RoleNotFoundError,
    SystemRoleImmutableError,
)
from hexcore.darwin.plugins.rbac.orms.sqlalchemy.repository import (  # noqa: E402
    SqlAlchemyAuthzVersionRepository,
    SqlAlchemyRbacPermissionRepository,
    SqlAlchemyRbacRoleRepository,
    SqlAlchemyRbacUserRoleRepository,
)
from hexcore.darwin.plugins.rbac.provider import RbacAuthorizationProvider  # noqa: E402
from hexcore.darwin.plugins.rbac.service import RbacService  # noqa: E402
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
def engine():
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor, plugins=["rbac"]))
    yield motor
    asyncio.run(dispose_engine())


@pytest.fixture
def servicio(engine) -> RbacService:
    return RbacService(
        roles=SqlAlchemyRbacRoleRepository(),
        permissions=SqlAlchemyRbacPermissionRepository(),
        user_roles=SqlAlchemyRbacUserRoleRepository(),
        versions=SqlAlchemyAuthzVersionRepository(),
        clock=FixedClock(AHORA),
    )


async def _usuario(email: str = "ana@ejemplo.com") -> User:
    return await SqlAlchemyUserRepository().add(User(email=email))


async def _admin(servicio: RbacService, email: str = "admin@ejemplo.com") -> User:
    """
    Un usuario con `"*"` efectivo, para poder ejercer el resto de la API sin que la propia
    anti-escalada lo bloquee.

    Bootstrapea con `assign_role(actor_id=None)` — la única forma de que alguien reciba su
    primer rol en un despliegue nuevo, porque hasta ese momento nadie tiene permisos con los
    que compararlo. Ver el docstring de `RbacService.assign_role`.
    """
    admin = await _usuario(email)
    rol = await servicio.create_role(name=f"admin-{email}")
    await servicio._permissions.ensure(["*"])
    await servicio._roles.set_permissions(rol.id, ["*"])
    await servicio.assign_role(actor_id=None, user_id=admin.id, role_id=rol.id, at=AHORA)
    return admin


class TestCRUDDeRoles:
    @pytest.mark.anyio
    async def test_crear_y_leer(self, servicio: RbacService):
        rol = await servicio.create_role(name="accountant")
        leido = await servicio.get_role(rol.id)

        assert leido.name == "accountant"
        assert leido.is_system is False

    @pytest.mark.anyio
    async def test_no_se_puede_editar_un_rol_de_sistema(self, servicio: RbacService):
        registry = RoleRegistry().register_role("viewer", permissions={"invoice.read"})
        await servicio.sync_system_roles(registry)
        sistema = await servicio.roles_by_ids(
            [r.id for r in await servicio.list_roles("") if r.name == "viewer"]
        )

        with pytest.raises(SystemRoleImmutableError):
            await servicio.update_role(sistema[0].id, name="otro")

        with pytest.raises(SystemRoleImmutableError):
            await servicio.delete_role(sistema[0].id)

    @pytest.mark.anyio
    async def test_get_role_inexistente_lanza(self, servicio: RbacService):
        with pytest.raises(RoleNotFoundError):
            await servicio.get_role(uuid4())


class TestAntiEscalada:
    @pytest.mark.anyio
    async def test_no_se_puede_otorgar_lo_que_no_se_tiene(self, servicio: RbacService):
        admin = await _admin(servicio)
        actor = await _usuario("actor@test.com")
        limitado = await servicio.create_role(name="limitado")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=limitado.id, permission_keys=["invoice.read"]
        )
        await servicio.assign_role(
            actor_id=admin.id, user_id=actor.id, role_id=limitado.id, at=AHORA
        )

        objetivo = await servicio.create_role(name="objetivo")
        with pytest.raises(EscalationError):
            await servicio.set_role_permissions(
                actor_id=actor.id,
                role_id=objetivo.id,
                permission_keys=["invoice.approve"],
            )

    @pytest.mark.anyio
    async def test_un_comodin_propio_cubre_lo_que_otorga(self, servicio: RbacService):
        """`grants()` reusado para la anti-escalada: `users.*` cubre otorgar `users.invite`."""
        admin = await _admin(servicio)
        actor = await _usuario("actor@test.com")
        amplio = await servicio.create_role(name="amplio")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=amplio.id, permission_keys=["users.*"]
        )
        await servicio.assign_role(
            actor_id=admin.id, user_id=actor.id, role_id=amplio.id, at=AHORA
        )

        objetivo = await servicio.create_role(name="objetivo")
        # No lanza: el actor tiene "users.*", que cubre "users.invite".
        await servicio.set_role_permissions(
            actor_id=actor.id, role_id=objetivo.id, permission_keys=["users.invite"]
        )

    @pytest.mark.anyio
    async def test_no_se_puede_asignar_un_rol_mas_amplio_que_el_propio(
        self, servicio: RbacService
    ):
        admin = await _admin(servicio)
        actor = await _usuario("actor@test.com")
        limitado = await servicio.create_role(name="limitado")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=limitado.id, permission_keys=["invoice.read"]
        )
        await servicio.assign_role(
            actor_id=admin.id, user_id=actor.id, role_id=limitado.id, at=AHORA
        )

        amplio = await servicio.create_role(name="amplio")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=amplio.id, permission_keys=["invoice.approve"]
        )

        otro = await _usuario("otro@test.com")
        with pytest.raises(EscalationError):
            await servicio.assign_role(
                actor_id=actor.id, user_id=otro.id, role_id=amplio.id, at=AHORA
            )

    @pytest.mark.anyio
    async def test_no_se_escala_agregando_un_padre_con_mas_permisos(
        self, servicio: RbacService
    ):
        """La anti-escalada de `set_role_parents` mira el efecto, no sólo lo directo."""
        admin = await _admin(servicio)
        actor = await _usuario("actor@test.com")
        limitado = await servicio.create_role(name="limitado")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=limitado.id, permission_keys=["invoice.read"]
        )
        await servicio.assign_role(
            actor_id=admin.id, user_id=actor.id, role_id=limitado.id, at=AHORA
        )

        poderoso = await servicio.create_role(name="poderoso")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=poderoso.id, permission_keys=["invoice.approve"]
        )

        hijo = await servicio.create_role(name="hijo")
        with pytest.raises(EscalationError):
            await servicio.set_role_parents(
                actor_id=actor.id, role_id=hijo.id, parent_ids=[poderoso.id]
            )


class TestCiclos:
    @pytest.mark.anyio
    async def test_un_rol_no_puede_heredar_de_si_mismo(self, servicio: RbacService):
        # `_admin` ya tiene "*" efectivo, así que la anti-escalada nunca tapa la detección
        # de ciclo en estos tests.
        actor = await _admin(servicio)
        todo = await servicio.create_role(name="todo")

        with pytest.raises(RoleCycleError):
            await servicio.set_role_parents(
                actor_id=actor.id, role_id=todo.id, parent_ids=[todo.id]
            )

    @pytest.mark.anyio
    async def test_ciclo_indirecto_se_rechaza(self, servicio: RbacService):
        actor = await _admin(servicio)

        a = await servicio.create_role(name="a")
        b = await servicio.create_role(name="b")
        await servicio.set_role_parents(actor_id=actor.id, role_id=b.id, parent_ids=[a.id])

        with pytest.raises(RoleCycleError):
            await servicio.set_role_parents(actor_id=actor.id, role_id=a.id, parent_ids=[b.id])


class TestPermisosEfectivos:
    @pytest.mark.anyio
    async def test_hereda_de_los_padres(self, servicio: RbacService):
        actor = await _admin(servicio)

        viewer = await servicio.create_role(name="viewer")
        await servicio.set_role_permissions(
            actor_id=actor.id, role_id=viewer.id, permission_keys=["invoice.read"]
        )
        editor = await servicio.create_role(name="editor")
        await servicio.set_role_permissions(
            actor_id=actor.id, role_id=editor.id, permission_keys=["invoice.write"]
        )
        await servicio.set_role_parents(
            actor_id=actor.id, role_id=editor.id, parent_ids=[viewer.id]
        )

        usuario = await _usuario("usuario@test.com")
        await servicio.assign_role(
            actor_id=actor.id, user_id=usuario.id, role_id=editor.id, at=AHORA
        )

        claves = await servicio.effective_permission_keys(usuario.id, "", at=AHORA)
        assert claves == frozenset({"invoice.write", "invoice.read"})

    @pytest.mark.anyio
    async def test_revocar_deja_de_conceder(self, servicio: RbacService):
        admin = await _admin(servicio)
        actor = await _usuario("actor@test.com")
        rol = await servicio.create_role(name="rol")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=rol.id, permission_keys=["invoice.read"]
        )
        await servicio.assign_role(actor_id=admin.id, user_id=actor.id, role_id=rol.id, at=AHORA)
        assert await servicio.effective_permission_keys(actor.id, "", at=AHORA)

        await servicio.revoke_role(user_id=actor.id, role_id=rol.id, scope_key="")
        assert await servicio.effective_permission_keys(actor.id, "", at=AHORA) == frozenset()

    @pytest.mark.anyio
    async def test_asignacion_vencida_no_concede(self, servicio: RbacService):
        admin = await _admin(servicio)
        actor = await _usuario("actor@test.com")
        rol = await servicio.create_role(name="rol")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=rol.id, permission_keys=["invoice.read"]
        )
        await servicio.assign_role(
            actor_id=admin.id,
            user_id=actor.id,
            role_id=rol.id,
            expires_at=AHORA + timedelta(minutes=1),
            at=AHORA,
        )

        vigente = await servicio.effective_permission_keys(actor.id, "", at=AHORA)
        vencido = await servicio.effective_permission_keys(
            actor.id, "", at=AHORA + timedelta(minutes=5)
        )
        assert vigente == frozenset({"invoice.read"})
        assert vencido == frozenset()


class TestSyncDeRolesDeCodigo:
    @pytest.mark.anyio
    async def test_sincroniza_permisos_y_herencia(self, servicio: RbacService):
        registry = (
            RoleRegistry()
            .register_role("viewer", permissions={"invoice.read"})
            .register_role("accountant", permissions={"invoice.approve"}, inherits={"viewer"})
        )
        await servicio.sync_system_roles(registry)

        roles = {r.name: r for r in await servicio.list_roles("")}
        assert roles["viewer"].is_system is True
        assert roles["accountant"].is_system is True

        claves = await servicio.roles_by_ids([roles["accountant"].id])
        assert claves[0].is_system is True

        heredado = await servicio._walk_role(roles["accountant"].id, visitados=set())
        assert heredado == {"invoice.approve", "invoice.read"}

    @pytest.mark.anyio
    async def test_correr_dos_veces_no_duplica(self, servicio: RbacService):
        registry = RoleRegistry().register_role("viewer", permissions={"invoice.read"})
        await servicio.sync_system_roles(registry)
        await servicio.sync_system_roles(registry)

        roles = [r for r in await servicio.list_roles("") if r.name == "viewer"]
        assert len(roles) == 1

    @pytest.mark.anyio
    async def test_un_permiso_sacado_del_registro_se_saca_del_rol(
        self, servicio: RbacService
    ):
        """`set_permissions` reemplaza: un rol de código que perdió un permiso lo pierde
        también en la tabla, no lo acumula."""
        registry = RoleRegistry().register_role(
            "viewer", permissions={"invoice.read", "invoice.export"}
        )
        await servicio.sync_system_roles(registry)

        registry2 = RoleRegistry().register_role("viewer", permissions={"invoice.read"})
        await servicio.sync_system_roles(registry2)

        rol = next(r for r in await servicio.list_roles("") if r.name == "viewer")
        assert await servicio._roles.permission_keys_for(rol.id) == frozenset(
            {"invoice.read"}
        )


class TestRbacAuthorizationProvider:
    def _request(self, user_id, action: str, scope: str = "") -> AccessRequest:
        actor = Principal(user_id=user_id, scopes=frozenset())
        contexto = AuthContext(actor=actor, subject=actor, transport="bearer")
        recurso = ResourceRef(type="invoice", scope_path=scope) if scope else None
        return AccessRequest(context=contexto, action=action, resource=recurso)

    @pytest.mark.anyio
    async def test_concede_lo_que_el_rol_da(self, servicio: RbacService, engine):
        admin = await _admin(servicio)
        actor = await _usuario("actor@test.com")
        rol = await servicio.create_role(name="rol")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=rol.id, permission_keys=["invoice.approve"]
        )
        await servicio.assign_role(actor_id=admin.id, user_id=actor.id, role_id=rol.id, at=AHORA)

        provider = RbacAuthorizationProvider(
            service=servicio, versions=SqlAlchemyAuthzVersionRepository(), clock=FixedClock(AHORA)
        )
        decision = await provider.decide(self._request(actor.id, "invoice.approve"))
        assert decision.effect == "allow"

        denegado = await provider.decide(self._request(actor.id, "users.delete"))
        assert denegado.effect == "not_applicable"

    @pytest.mark.anyio
    async def test_system_principal_no_aplica(self, servicio: RbacService, engine):
        from hexcore.darwin.domain.context import SystemPrincipal

        provider = RbacAuthorizationProvider(
            service=servicio, versions=SqlAlchemyAuthzVersionRepository(), clock=FixedClock(AHORA)
        )
        sistema = SystemPrincipal(name="cron:x", scopes=frozenset({"invoice.approve"}))
        contexto = AuthContext(actor=sistema, subject=sistema, transport="internal")
        decision = await provider.decide(
            AccessRequest(context=contexto, action="invoice.approve")
        )
        assert decision.effect == "not_applicable"

    @pytest.mark.anyio
    async def test_revocar_el_rol_afecta_la_decision_siguiente(
        self, servicio: RbacService, engine
    ):
        """
        DoD de la Fase F2: quitar un rol afecta la siguiente request. La clave de cache lleva
        la versión adentro, así que no hace falta invalidar nada a mano.
        """
        admin = await _admin(servicio)
        actor = await _usuario("actor@test.com")
        rol = await servicio.create_role(name="rol")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=rol.id, permission_keys=["invoice.approve"]
        )
        await servicio.assign_role(actor_id=admin.id, user_id=actor.id, role_id=rol.id, at=AHORA)

        versiones = SqlAlchemyAuthzVersionRepository()
        provider = RbacAuthorizationProvider(
            service=servicio, versions=versiones, clock=FixedClock(AHORA)
        )

        antes = await provider.decide(self._request(actor.id, "invoice.approve"))
        assert antes.effect == "allow"

        await servicio.revoke_role(user_id=actor.id, role_id=rol.id, scope_key="")

        despues = await provider.decide(self._request(actor.id, "invoice.approve"))
        assert despues.effect == "not_applicable"


class TestRbacPlugin:
    def test_contributed_tables_coincide_con_tables(self):
        plugin = RbacPlugin()
        assert tuple(RbacPlugin.contributed_tables) == tuple(plugin.tables())

    def test_exception_status_map(self):
        from hexcore.darwin.plugins.rbac.domain import RBAC_EXCEPTION_STATUS_MAP

        plugin = RbacPlugin()
        assert plugin.exception_status_map() == RBAC_EXCEPTION_STATUS_MAP

    def test_incluye_un_provider_de_autorizacion(self, engine):
        reset_identity()
        try:
            plugin = RbacPlugin()
            configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key=CLAVE),
                plugins=PluginRegistry([plugin]),
                clock=FixedClock(AHORA),
                key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            )
            proveedores = plugin.authorization_providers()
            assert len(proveedores) == 1
            assert proveedores[0].name == "rbac"
        finally:
            reset_identity()

    def test_incluye_un_startup_step(self):
        plugin = RbacPlugin()
        pasos = plugin.startup_steps()
        assert len(pasos) == 1
        assert pasos[0].name == "darwin-rbac-seed"

    def test_incluye_un_router(self):
        plugin = RbacPlugin()
        assert len(plugin.routers()) == 1

    def test_sin_router_si_include_router_es_false(self):
        plugin = RbacPlugin(include_router=False)
        assert plugin.routers() == ()

    @pytest.mark.anyio
    async def test_cableado_completo_resuelve_y_sincroniza(self, engine):
        pytest.importorskip("fastapi")

        registry = RoleRegistry().register_role("viewer", permissions={"invoice.read"})
        reset_identity()
        try:
            plugin = RbacPlugin(registry=registry)
            configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key=CLAVE),
                plugins=PluginRegistry([plugin]),
                clock=FixedClock(AHORA),
                key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            )

            for paso in plugin.startup_steps():
                await paso.start()

            servicio = get_rbac_service()
            roles = await servicio.list_roles("")
            assert any(r.name == "viewer" and r.is_system for r in roles)

            from hexcore.darwin.application.container import get_identity_container

            authorizer = get_identity_container().authorizer()
            assert any(
                getattr(p, "name", "") == "rbac" for p in authorizer._providers
            )
        finally:
            reset_identity()
