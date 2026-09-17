"""
Darwin — Fase F1 del plan rbac/drbac: dominio y persistencia de `rbac`.

Lo que se fija:

1. `CompiledPermissionSet` tiene la **misma semántica** que `Permission.grants`: exacto,
   comodín, comodín total, y el caso que casi siempre se rompe — un comodín no concede el nodo
   padre pelado (`"users.*"` no concede `"users"`).
2. El dominio (`RbacPermission`, `RoleAssignment`) valida lo que le corresponde a él y nada
   más.
3. Los cuatro repositorios de SQLAlchemy, contra SQLite real: CRUD de roles, el catálogo de
   permisos, las asignaciones (incluida la expiración) y el contador de versión — con
   `AuthzVersionRepository.bump()` probado bajo concurrencia real, que es la razón de que sea
   una sola sentencia y no leer-sumar-escribir.
"""
from __future__ import annotations

import asyncio
import itertools
import random
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.darwin import User, create_identity_tables  # noqa: E402
from hexcore.darwin.domain.permissions import Permission  # noqa: E402
from hexcore.darwin.infrastructure.orms.sqlalchemy.repositories import (  # noqa: E402
    SqlAlchemyUserRepository,
)
from hexcore.darwin.plugins.rbac.domain import (  # noqa: E402
    RbacPermission,
    RbacRole,
    RoleAssignment,
    RoleNotFoundError,
)
from hexcore.darwin.plugins.rbac.matcher import CompiledPermissionSet  # noqa: E402
from hexcore.darwin.plugins.rbac.orms.sqlalchemy.repository import (  # noqa: E402
    SqlAlchemyAuthzVersionRepository,
    SqlAlchemyRbacPermissionRepository,
    SqlAlchemyRbacRoleRepository,
    SqlAlchemyRbacUserRoleRepository,
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


# ── CompiledPermissionSet ──────────────────────────────────────────────────────
class TestCompiledPermissionSet:
    def test_permiso_exacto(self):
        compilado = CompiledPermissionSet.compile({"invoice.read"})

        assert compilado.grants("invoice.read") is True
        assert compilado.grants("invoice.write") is False

    def test_comodin_concede_descendientes(self):
        compilado = CompiledPermissionSet.compile({"users.*"})

        assert compilado.grants("users.invite") is True
        assert compilado.grants("users.invite.bulk") is True

    def test_comodin_no_concede_el_nodo_pelado(self):
        """El caso que casi siempre se rompe: `"users.*"` no concede `"users"`."""
        compilado = CompiledPermissionSet.compile({"users.*"})

        assert compilado.grants("users") is False

    def test_comodin_no_concede_un_hermano(self):
        compilado = CompiledPermissionSet.compile({"users.*"})

        assert compilado.grants("invoice.read") is False

    def test_comodin_total(self):
        compilado = CompiledPermissionSet.compile({"*"})

        assert compilado.grants("cualquier.cosa") is True
        assert compilado.grants("otra.cosa.mas.anidada") is True

    def test_conjunto_vacio_no_concede_nada(self):
        compilado = CompiledPermissionSet.compile(())

        assert compilado.grants("invoice.read") is False

    def test_mezcla_de_exactos_y_comodines(self):
        compilado = CompiledPermissionSet.compile({"invoice.read", "users.*"})

        assert compilado.grants("invoice.read") is True
        assert compilado.grants("users.invite") is True
        assert compilado.grants("invoice.write") is False

    def test_equivalencia_con_permission_grants(self):
        """
        `CompiledPermissionSet.grants` ≡ `any(Permission(v).grants(...) for v in conjunto)`,
        para un muestreo de conjuntos y de permisos pedidos. No es hypothesis, pero cubre la
        misma pregunta: que compilar no cambie la semántica.
        """
        recursos = ("users", "invoice", "billing", "reports")
        acciones = ("read", "write", "invite", "approve", "delete")
        universo = [f"{r}.{a}" for r, a in itertools.product(recursos, acciones)]
        universo += [f"{r}.*" for r in recursos] + ["*"]

        rng = random.Random(1234)
        for _ in range(200):
            conjunto = set(rng.sample(universo, k=rng.randint(0, 5)))
            pedido = rng.choice(universo + list(recursos))

            compilado = CompiledPermissionSet.compile(conjunto)
            esperado = any(Permission(value=v).grants(pedido) for v in conjunto)

            assert compilado.grants(pedido) is esperado, (conjunto, pedido)


# ── El dominio ──────────────────────────────────────────────────────────────
class TestRbacPermission:
    @pytest.mark.parametrize("clave", ["invoice.approve", "invoice.*", "*"])
    def test_formas_validas(self, clave: str):
        RbacPermission(id=uuid4(), key=clave)

    @pytest.mark.parametrize("clave", ["invoice", "", ".", "invoice.", ".approve"])
    def test_formas_invalidas_se_rechazan(self, clave: str):
        with pytest.raises(ValueError):
            RbacPermission(id=uuid4(), key=clave)


class TestRoleAssignment:
    def test_sin_vencimiento_nunca_expira(self):
        asignacion = RoleAssignment(id=uuid4(), user_id=uuid4(), role_id=uuid4())

        assert asignacion.is_expired_at(AHORA + timedelta(days=3650)) is False

    def test_con_vencimiento_expira_en_el_instante_exacto(self):
        vence = AHORA + timedelta(hours=1)
        asignacion = RoleAssignment(
            id=uuid4(), user_id=uuid4(), role_id=uuid4(), expires_at=vence
        )

        assert asignacion.is_expired_at(vence - timedelta(seconds=1)) is False
        assert asignacion.is_expired_at(vence) is True


# ── Persistencia: SQLAlchemy ──────────────────────────────────────────────────
@pytest.fixture
def engine():
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor, plugins=["rbac"]))
    yield motor
    asyncio.run(dispose_engine())


@pytest.fixture
def roles(engine):
    return SqlAlchemyRbacRoleRepository()


@pytest.fixture
def permisos(engine):
    return SqlAlchemyRbacPermissionRepository()


@pytest.fixture
def asignaciones(engine):
    return SqlAlchemyRbacUserRoleRepository()


@pytest.fixture
def versiones(engine):
    return SqlAlchemyAuthzVersionRepository()


async def _crear_usuario(email: str = "ana@ejemplo.com"):
    return await SqlAlchemyUserRepository().add(User(email=email))


def _rol(**overrides) -> RbacRole:
    base = dict(id=uuid4(), name="accountant")
    base.update(overrides)
    return RbacRole(**base)


class TestRoleRepository:
    @pytest.mark.anyio
    async def test_alta_y_lectura(self, roles):
        creado = await roles.add(_rol())
        leido = await roles.get(creado.id)

        assert leido is not None
        assert leido.name == "accountant"
        assert leido.is_system is False
        assert leido.created_at is not None

    @pytest.mark.anyio
    async def test_get_by_name_respeta_el_scope(self, roles):
        await roles.add(_rol(name="admin", scope_key="org:1"))
        await roles.add(_rol(name="admin", scope_key="org:2"))

        assert (await roles.get_by_name("org:1", "admin")) is not None
        assert (await roles.get_by_name("org:3", "admin")) is None

    @pytest.mark.anyio
    async def test_list_for_scope_no_trae_otros_scopes(self, roles):
        await roles.add(_rol(name="a", scope_key="org:1"))
        await roles.add(_rol(name="b", scope_key="org:2"))

        listado = await roles.list_for_scope("org:1")

        assert [r.name for r in listado] == ["a"]

    @pytest.mark.anyio
    async def test_update_inexistente_lanza(self, roles):
        with pytest.raises(RoleNotFoundError):
            await roles.update(_rol(id=uuid4()))

    @pytest.mark.anyio
    async def test_update_cambia_nombre_y_descripcion(self, roles):
        creado = await roles.add(_rol())
        actualizado = await roles.update(
            creado.model_copy(update={"name": "senior_accountant", "description": "x"})
        )

        assert actualizado.name == "senior_accountant"
        assert actualizado.description == "x"

    @pytest.mark.anyio
    async def test_delete(self, roles):
        creado = await roles.add(_rol())

        assert await roles.delete(creado.id) is True
        assert await roles.get(creado.id) is None
        assert await roles.delete(creado.id) is False

    @pytest.mark.anyio
    async def test_dos_roles_con_el_mismo_nombre_y_scope_violan_unique(self, roles):
        from sqlalchemy.exc import IntegrityError

        await roles.add(_rol(name="admin"))
        with pytest.raises(IntegrityError):
            await roles.add(_rol(name="admin"))

    @pytest.mark.anyio
    async def test_permisos_directos_se_reemplazan_no_se_acumulan(self, roles, permisos):
        await permisos.ensure({"invoice.read", "invoice.approve", "users.*"})
        creado = await roles.add(_rol())

        await roles.set_permissions(creado.id, ["invoice.read"])
        assert await roles.permission_keys_for(creado.id) == frozenset({"invoice.read"})

        await roles.set_permissions(creado.id, ["invoice.approve", "users.*"])
        assert await roles.permission_keys_for(creado.id) == frozenset(
            {"invoice.approve", "users.*"}
        )

    @pytest.mark.anyio
    async def test_permisos_directos_vacio_por_defecto(self, roles):
        creado = await roles.add(_rol())

        assert await roles.permission_keys_for(creado.id) == frozenset()

    @pytest.mark.anyio
    async def test_padres_se_reemplazan_no_se_acumulan(self, roles):
        viewer = await roles.add(_rol(name="viewer"))
        editor = await roles.add(_rol(name="editor"))
        admin = await roles.add(_rol(name="admin"))

        await roles.set_parents(editor.id, [viewer.id])
        assert await roles.parent_ids_for(editor.id) == frozenset({viewer.id})

        await roles.set_parents(editor.id, [viewer.id, admin.id])
        assert await roles.parent_ids_for(editor.id) == frozenset({viewer.id, admin.id})

    @pytest.mark.anyio
    async def test_borrar_un_rol_borra_sus_permisos_y_padres_por_cascade(self, roles, permisos):
        await permisos.ensure({"invoice.read"})
        padre = await roles.add(_rol(name="viewer"))
        hijo = await roles.add(_rol(name="editor"))
        await roles.set_permissions(hijo.id, ["invoice.read"])
        await roles.set_parents(hijo.id, [padre.id])

        await roles.delete(hijo.id)

        # Las filas de unión del rol borrado no quedan huérfanas: CASCADE las limpió.
        nuevo = await roles.add(_rol(name="editor"))
        assert await roles.permission_keys_for(nuevo.id) == frozenset()
        assert await roles.parent_ids_for(nuevo.id) == frozenset()


class TestPermissionRepository:
    @pytest.mark.anyio
    async def test_ensure_es_idempotente(self, permisos):
        await permisos.ensure({"invoice.read", "invoice.approve"})
        await permisos.ensure({"invoice.read", "users.*"})

        claves = {p.key for p in await permisos.list_all()}
        assert claves == {"invoice.read", "invoice.approve", "users.*"}

    @pytest.mark.anyio
    async def test_ensure_de_vacio_no_hace_nada(self, permisos):
        await permisos.ensure(())

        assert await permisos.list_all() == []

    @pytest.mark.anyio
    async def test_get_by_key(self, permisos):
        await permisos.ensure({"invoice.read"})

        encontrado = await permisos.get_by_key("invoice.read")
        assert encontrado is not None
        assert encontrado.key == "invoice.read"
        assert await permisos.get_by_key("no.existe") is None

    @pytest.mark.anyio
    async def test_add_directo(self, permisos):
        creado = await permisos.add(
            RbacPermission(id=uuid4(), key="billing.refund", description="Emite un reembolso")
        )

        assert creado.description == "Emite un reembolso"


class TestUserRoleRepository:
    @pytest.mark.anyio
    async def test_assign_y_list_for_user(self, roles, asignaciones):
        usuario = await _crear_usuario()
        rol = await roles.add(_rol())

        asignada = await asignaciones.assign(
            RoleAssignment(id=uuid4(), user_id=usuario.id, role_id=rol.id)
        )

        listado = await asignaciones.list_for_user(usuario.id, "")
        assert [a.id for a in listado] == [asignada.id]

    @pytest.mark.anyio
    async def test_revoke(self, roles, asignaciones):
        usuario = await _crear_usuario()
        rol = await roles.add(_rol())
        await asignaciones.assign(RoleAssignment(id=uuid4(), user_id=usuario.id, role_id=rol.id))

        assert await asignaciones.revoke(usuario.id, rol.id, "") is True
        assert await asignaciones.revoke(usuario.id, rol.id, "") is False
        assert await asignaciones.list_for_user(usuario.id, "") == []

    @pytest.mark.anyio
    async def test_active_role_ids_for_excluye_vencidas(self, roles, asignaciones):
        usuario = await _crear_usuario()
        vigente = await roles.add(_rol(name="vigente"))
        vencido = await roles.add(_rol(name="vencido"))

        await asignaciones.assign(
            RoleAssignment(id=uuid4(), user_id=usuario.id, role_id=vigente.id)
        )
        await asignaciones.assign(
            RoleAssignment(
                id=uuid4(),
                user_id=usuario.id,
                role_id=vencido.id,
                expires_at=AHORA - timedelta(minutes=1),
            )
        )

        activos = await asignaciones.active_role_ids_for(usuario.id, "", at=AHORA)
        assert activos == {vigente.id}

    @pytest.mark.anyio
    async def test_active_role_ids_for_respeta_el_scope(self, roles, asignaciones):
        usuario = await _crear_usuario()
        rol = await roles.add(_rol(scope_key="org:1"))
        await asignaciones.assign(
            RoleAssignment(
                id=uuid4(), user_id=usuario.id, role_id=rol.id, scope_key="org:1"
            )
        )

        assert await asignaciones.active_role_ids_for(usuario.id, "org:1", at=AHORA) == {
            rol.id
        }
        assert await asignaciones.active_role_ids_for(usuario.id, "org:2", at=AHORA) == set()

    @pytest.mark.anyio
    async def test_asignar_el_mismo_rol_dos_veces_en_el_mismo_scope_viola_unique(
        self, roles, asignaciones
    ):
        from sqlalchemy.exc import IntegrityError

        usuario = await _crear_usuario()
        rol = await roles.add(_rol())
        await asignaciones.assign(RoleAssignment(id=uuid4(), user_id=usuario.id, role_id=rol.id))

        with pytest.raises(IntegrityError):
            await asignaciones.assign(
                RoleAssignment(id=uuid4(), user_id=usuario.id, role_id=rol.id)
            )


class TestAuthzVersionRepository:
    @pytest.mark.anyio
    async def test_get_de_un_scope_que_nunca_bumpeo_es_cero(self, versiones):
        assert await versiones.get("org:nuevo") == 0

    @pytest.mark.anyio
    async def test_bump_crea_y_sube(self, versiones):
        assert await versiones.bump("org:1") == 1
        assert await versiones.bump("org:1") == 2
        assert await versiones.get("org:1") == 2

    @pytest.mark.anyio
    async def test_scopes_distintos_no_se_pisan(self, versiones):
        await versiones.bump("org:1")
        await versiones.bump("org:1")
        await versiones.bump("org:2")

        assert await versiones.get("org:1") == 2
        assert await versiones.get("org:2") == 1

    @pytest.mark.anyio
    async def test_bump_concurrente_no_pierde_incrementos(self, versiones):
        """
        La razón de que `bump` sea un upsert atómico y no leer-sumar-escribir: con N bumps
        concurrentes en el mismo scope, tienen que verse los N incrementos y no menos.
        """
        await asyncio.gather(*(versiones.bump("org:concurrente") for _ in range(20)))

        assert await versiones.get("org:concurrente") == 20
