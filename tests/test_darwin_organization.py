"""
Darwin Fase 9: `organization`, contra SQLite y la app real.

El plugin existe para sostener tres invariantes, y este archivo prueba sobre todo que **no se
pueden romper**:

- **Una organización nunca queda sin `owner`.** Ni sacándolo, ni degradándolo, ni con dos
  peticiones concurrentes que degradan a los dos últimos.
- **Nadie asciende a alguien por encima de sí mismo, ni actúa sobre un par o un superior.** Cubre
  invitar con un rol mayor al propio, ascenderse a sí mismo, y degradar a un par.
- **La invitación está atada al mail, y el mail tiene que estar verificado.** Reenviar el link es
  lo que la gente hace.

Y lo demás que se fija: el token de invitación va hasheado, el canje es atómico, revocar deja
rastro, irse uno mismo no requiere permiso, la lista de miembros no es pública, y el slug no se
puede cambiar.
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
    TokenConfig,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    reset_identity,
)
from hexcore.darwin.plugins.organization import (  # noqa: E402
    AlreadyAMemberError,
    InsufficientOrgRoleError,
    InvitationEmailMismatchError,
    InvitationError,
    InvitationStatus,
    LastOwnerError,
    NotAMemberError,
    OrganizationError,
    OrganizationNotFoundError,
    OrganizationPlugin,
    OrgRole,
    SlugAlreadyTakenError,
    get_organization_service,
    slugify,
)
from hexcore.darwin.plugins.organization.models import (  # noqa: E402
    create_organization_tables,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48
PASS = "una frase larga y buena"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Los roles ─────────────────────────────────────────────────────────────────
class TestOrgRole:
    def test_el_orden_es_el_correcto(self):
        """
        ⚠️ El orden alfabético de `admin` < `member` < `owner` es exactamente el equivocado. Por eso
        `rank` existe y no se comparan los nombres.
        """
        assert OrgRole.OWNER.rank > OrgRole.ADMIN.rank > OrgRole.MEMBER.rank

    def test_el_orden_alfabetico_seria_incorrecto(self):
        """El test que documenta por qué `rank` no es cosmético."""
        alfabetico = sorted(r.value for r in OrgRole)

        assert alfabetico == ["admin", "member", "owner"]
        assert alfabetico != [r.value for r in sorted(OrgRole, key=lambda r: r.rank)]

    @pytest.mark.parametrize(
        "uno, otro, esperado",
        [
            (OrgRole.OWNER, OrgRole.ADMIN, True),
            (OrgRole.OWNER, OrgRole.MEMBER, True),
            (OrgRole.ADMIN, OrgRole.MEMBER, True),
            (OrgRole.ADMIN, OrgRole.OWNER, False),
            (OrgRole.MEMBER, OrgRole.ADMIN, False),
            (OrgRole.ADMIN, OrgRole.ADMIN, False),
        ],
    )
    def test_outranks_es_estricto(self, uno, otro, esperado):
        """Estricto a propósito: un par no administra a un par."""
        assert uno.outranks(otro) is esperado

    def test_at_least_no_es_estricto(self):
        assert OrgRole.ADMIN.at_least(OrgRole.ADMIN) is True
        assert OrgRole.ADMIN.at_least(OrgRole.MEMBER) is True
        assert OrgRole.MEMBER.at_least(OrgRole.ADMIN) is False


class TestSlugify:
    @pytest.mark.parametrize(
        "entrada, salida",
        [
            ("Mi Empresa", "mi-empresa"),
            ("Mi Empresa S.A.", "mi-empresa-s-a"),
            ("  espacios  ", "espacios"),
            ("MAYUSCULAS", "mayusculas"),
            ("guiones---repetidos", "guiones-repetidos"),
            ("---bordes---", "bordes"),
            ("acentuación", "acentuaci-n"),
            ("", "org"),
            ("!!!", "org"),
        ],
    )
    def test_slugs(self, entrada, salida):
        assert slugify(entrada) == salida

    def test_nunca_deja_caracteres_de_url(self):
        """
        Deliberadamente pobre —no translitera acentos— pero lo que no hace es dejar pasar
        caracteres que después aparecen escapados en una URL.
        """
        for basura in ("a/b", "a?b", "a#b", "a b", "a%b", "ñandú"):
            assert set(slugify(basura)) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")


# ── El cableado ───────────────────────────────────────────────────────────────
@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def plugin() -> OrganizationPlugin:
    return OrganizationPlugin()


@pytest.fixture
def contenedor(reloj, plugin):
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))
    asyncio.run(create_organization_tables(motor))

    from hexcore.config import LazyConfig
    from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

    LazyConfig.get_config().cache_backend = MemoryCache()

    reset_identity()
    plugin.reset()
    contenedor = configure_identity(
        IdentityConfig(
            secret_key=CLAVE,
            tokens=TokenConfig(issuer="https://api.test"),
            require_verified_email=False,
        ),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        plugins=PluginRegistry([plugin]),
    )
    yield contenedor

    reset_identity()
    plugin.reset()
    asyncio.run(dispose_engine())


@pytest.fixture
def servicio(contenedor):
    return get_organization_service()


async def _usuario(contenedor, email: str, *, verificado: bool = True):
    usuario, _ = await contenedor.identity_service().sign_up(
        email=email, password=PASS
    )
    return await contenedor.users().update(
        usuario.model_copy(update={"email_verified": verificado})
    )


async def _con_equipo(contenedor, servicio):
    """
    Una organización con un `owner`, un `admin` y un `member`. Es el escenario de casi todo test de
    autorización.
    """
    dueno = await _usuario(contenedor, "dueno@ejemplo.com")
    admin = await _usuario(contenedor, "admin@ejemplo.com")
    miembro = await _usuario(contenedor, "miembro@ejemplo.com")

    org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
    for usuario, rol in ((admin, OrgRole.ADMIN), (miembro, OrgRole.MEMBER)):
        emitida = await servicio.invite(
            organization_id=org.id,
            actor_id=dueno.id,
            email=usuario.email,
            role=rol,
        )
        await servicio.accept_invitation(token=emitida.token, user_id=usuario.id)

    return org, dueno, admin, miembro


# ── Crear ─────────────────────────────────────────────────────────────────────
class TestCrear:
    @pytest.mark.anyio
    async def test_el_creador_queda_owner(self, contenedor, servicio):
        """
        En el mismo flujo y no en un paso aparte: una organización sin `owner` es inadministrable
        desde el segundo cero, y dos pasos separados garantizan que alguna vez uno falle en el
        medio.
        """
        usuario = await _usuario(contenedor, "ana@ejemplo.com")

        org = await servicio.create(name="Mi Empresa", owner_id=usuario.id)

        membresia = await servicio.require_role(
            organization_id=org.id, user_id=usuario.id, minimum=OrgRole.OWNER
        )
        assert membresia.role is OrgRole.OWNER
        assert org.slug == "mi-empresa"

    @pytest.mark.anyio
    async def test_un_slug_propio_se_respeta(self, contenedor, servicio):
        usuario = await _usuario(contenedor, "ana@ejemplo.com")

        org = await servicio.create(
            name="Mi Empresa", owner_id=usuario.id, slug="acme"
        )

        assert org.slug == "acme"
        assert (await servicio.get_by_slug("acme")).id == org.id

    @pytest.mark.anyio
    async def test_un_slug_repetido_se_rechaza(self, contenedor, servicio):
        usuario = await _usuario(contenedor, "ana@ejemplo.com")
        await servicio.create(name="Mi Empresa", owner_id=usuario.id, slug="acme")

        with pytest.raises(SlugAlreadyTakenError, match="acme"):
            await servicio.create(name="Otra", owner_id=usuario.id, slug="acme")

    @pytest.mark.anyio
    async def test_la_metadata_se_guarda(self, contenedor, servicio):
        usuario = await _usuario(contenedor, "ana@ejemplo.com")

        org = await servicio.create(
            name="Mi Empresa", owner_id=usuario.id, metadata={"plan": "pro"}
        )

        assert (await servicio.get(org.id)).metadata == {"plan": "pro"}

    @pytest.mark.anyio
    async def test_una_organizacion_inexistente_da_not_found(self, servicio):
        with pytest.raises(OrganizationNotFoundError):
            await servicio.get(uuid4())
        with pytest.raises(OrganizationNotFoundError):
            await servicio.get_by_slug("no-existe")

    @pytest.mark.anyio
    async def test_el_slug_no_se_puede_cambiar(self, contenedor, servicio):
        """
        Un slug que cambia rompe cada link guardado, cada bookmark y cada integración que lo tenga
        fijo. El `update` sólo toca nombre y metadata.
        """
        usuario = await _usuario(contenedor, "ana@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=usuario.id)

        actualizada = await servicio.update(
            organization_id=org.id,
            actor_id=usuario.id,
            name="Nombre Nuevo",
            metadata={"plan": "enterprise"},
        )

        assert actualizada.name == "Nombre Nuevo"
        assert actualizada.metadata == {"plan": "enterprise"}
        assert actualizada.slug == org.slug, "el slug no se movió"


# ── Autorización ──────────────────────────────────────────────────────────────
class TestAutorizacion:
    @pytest.mark.anyio
    async def test_un_no_miembro_no_pasa(self, contenedor, servicio):
        org, *_ = await _con_equipo(contenedor, servicio)
        ajeno = await _usuario(contenedor, "ajeno@ejemplo.com")

        with pytest.raises(NotAMemberError):
            await servicio.require_role(
                organization_id=org.id, user_id=ajeno.id, minimum=OrgRole.MEMBER
            )

    @pytest.mark.anyio
    async def test_un_member_no_llega_a_admin(self, contenedor, servicio):
        org, _, _, miembro = await _con_equipo(contenedor, servicio)

        with pytest.raises(InsufficientOrgRoleError, match="admin"):
            await servicio.require_role(
                organization_id=org.id, user_id=miembro.id, minimum=OrgRole.ADMIN
            )

    @pytest.mark.anyio
    async def test_un_owner_llega_a_todo(self, contenedor, servicio):
        org, dueno, _, _ = await _con_equipo(contenedor, servicio)

        for minimo in (OrgRole.MEMBER, OrgRole.ADMIN, OrgRole.OWNER):
            membresia = await servicio.require_role(
                organization_id=org.id, user_id=dueno.id, minimum=minimo
            )
            assert membresia.role is OrgRole.OWNER

    @pytest.mark.anyio
    async def test_la_lista_de_miembros_no_es_publica(self, contenedor, servicio):
        """
        Un endpoint que la devuelve sin chequear es una fuente de datos para prospección y para
        ingeniería social.
        """
        org, _, _, miembro = await _con_equipo(contenedor, servicio)
        ajeno = await _usuario(contenedor, "ajeno@ejemplo.com")

        with pytest.raises(NotAMemberError):
            await servicio.list_members(organization_id=org.id, actor_id=ajeno.id)

        # Y un `member` cualquiera sí la ve: lo que se exige es pertenecer, no administrar.
        desde_adentro = await servicio.list_members(
            organization_id=org.id, actor_id=miembro.id
        )
        assert len(desde_adentro) == 3


# ── Invitar ───────────────────────────────────────────────────────────────────
class TestInvitar:
    @pytest.mark.anyio
    async def test_el_flujo_completo(self, contenedor, servicio):
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        invitado = await _usuario(contenedor, "beto@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)

        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email="beto@ejemplo.com"
        )
        assert emitida.token
        assert emitida.invitation.status is InvitationStatus.PENDING
        assert emitida.invitation.expires_at == AHORA + timedelta(days=7)

        miembro = await servicio.accept_invitation(
            token=emitida.token, user_id=invitado.id
        )

        assert miembro.organization_id == org.id
        assert miembro.role is OrgRole.MEMBER

    @pytest.mark.anyio
    async def test_el_token_se_guarda_hasheado(self, contenedor, servicio):
        """
        El link viaja por mail y queda en el buzón, en los logs del proveedor y en el historial del
        cliente: un dump de la tabla no debería sumar la capacidad de entrar a una organización
        ajena.
        """
        from sqlalchemy import select

        from hexcore.darwin.infrastructure.hashing import hash_token
        from hexcore.darwin.plugins.organization.models import InvitationModel
        from hexcore.infrastructure.uow.scopes import session_scope

        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email="beto@ejemplo.com"
        )

        async with session_scope() as sesion:
            fila = (await sesion.execute(select(InvitationModel))).scalar_one()

        assert fila.token_hash == hash_token(emitida.token)
        assert emitida.token not in fila.token_hash

    @pytest.mark.anyio
    async def test_un_member_no_puede_invitar(self, contenedor, servicio):
        org, _, _, miembro = await _con_equipo(contenedor, servicio)

        with pytest.raises(InsufficientOrgRoleError):
            await servicio.invite(
                organization_id=org.id,
                actor_id=miembro.id,
                email="nuevo@ejemplo.com",
            )

    @pytest.mark.anyio
    async def test_un_admin_no_puede_invitar_como_owner(self, contenedor, servicio):
        """
        ⚠️ La escalada más barata del modelo: un `admin` invita a un cómplice como `owner` y desde
        ahí lo tiene todo. Y pasa por un endpoint que suena inofensivo.
        """
        org, _, admin, _ = await _con_equipo(contenedor, servicio)

        with pytest.raises(InsufficientOrgRoleError, match="por encima de sí mismo"):
            await servicio.invite(
                organization_id=org.id,
                actor_id=admin.id,
                email="complice@ejemplo.com",
                role=OrgRole.OWNER,
            )

    @pytest.mark.anyio
    async def test_un_admin_si_puede_invitar_como_admin(self, contenedor, servicio):
        """Hasta su propio nivel, sí: `outranks` es estricto."""
        org, _, admin, _ = await _con_equipo(contenedor, servicio)

        emitida = await servicio.invite(
            organization_id=org.id,
            actor_id=admin.id,
            email="otro-admin@ejemplo.com",
            role=OrgRole.ADMIN,
        )

        assert emitida.invitation.role is OrgRole.ADMIN

    @pytest.mark.anyio
    async def test_invitar_a_un_miembro_existente_falla(self, contenedor, servicio):
        org, dueno, admin, _ = await _con_equipo(contenedor, servicio)

        with pytest.raises(AlreadyAMemberError):
            await servicio.invite(
                organization_id=org.id, actor_id=dueno.id, email=admin.email
            )

    @pytest.mark.anyio
    async def test_el_mail_se_normaliza(self, contenedor, servicio):
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)

        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email="BETO@Ejemplo.COM"
        )

        assert emitida.invitation.email == "beto@ejemplo.com"

    @pytest.mark.anyio
    async def test_el_techo_de_miembros_se_respeta(self, contenedor, reloj):
        """Existe apagado por default; el test prueba que el parámetro sirve."""
        plugin = OrganizationPlugin(max_members=2)
        reset_identity()
        cont = configure_identity(
            IdentityConfig(secret_key=CLAVE, require_verified_email=False),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            plugins=PluginRegistry([plugin]),
        )
        try:
            servicio = plugin.service()
            dueno = await _usuario(cont, "dueno@ejemplo.com")
            beto = await _usuario(cont, "beto@ejemplo.com")
            org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)

            emitida = await servicio.invite(
                organization_id=org.id, actor_id=dueno.id, email=beto.email
            )
            await servicio.accept_invitation(token=emitida.token, user_id=beto.id)

            with pytest.raises(ValueError, match="máximo de 2"):
                await servicio.invite(
                    organization_id=org.id,
                    actor_id=dueno.id,
                    email="tercero@ejemplo.com",
                )
        finally:
            reset_identity()


# ── Aceptar: la invitación atada al mail ──────────────────────────────────────
class TestAceptar:
    @pytest.mark.anyio
    async def test_reenviar_el_link_no_sirve(self, contenedor, servicio):
        """
        ⚠️ **El test del abuso más común del flujo.** Reenviar el link de invitación es exactamente
        lo que la gente hace; sin el chequeo del mail, quien lo reciba entra con el rol que se le
        había dado a otro.
        """
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        intruso = await _usuario(contenedor, "intruso@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)

        emitida = await servicio.invite(
            organization_id=org.id,
            actor_id=dueno.id,
            email="beto@ejemplo.com",
            role=OrgRole.ADMIN,
        )

        with pytest.raises(InvitationEmailMismatchError, match="otra dirección"):
            await servicio.accept_invitation(
                token=emitida.token, user_id=intruso.id
            )

    @pytest.mark.anyio
    async def test_el_rechazo_no_gasta_la_invitacion(self, contenedor, servicio):
        """
        El mail se chequea **antes** de consumir: consumirla primero y rechazar después la
        gastaría, y el invitado legítimo tendría que pedir otra por un intento que no era suyo.
        """
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        intruso = await _usuario(contenedor, "intruso@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email=beto.email
        )

        with pytest.raises(InvitationEmailMismatchError):
            await servicio.accept_invitation(token=emitida.token, user_id=intruso.id)

        # Y el invitado legítimo la puede usar igual.
        miembro = await servicio.accept_invitation(
            token=emitida.token, user_id=beto.id
        )
        assert miembro.user_id == beto.id

    @pytest.mark.anyio
    async def test_un_mail_sin_verificar_no_acepta(self, contenedor, servicio):
        """
        ⚠️ Sin esto, alguien registra una cuenta con el mail de un invitado y le roba la invitación
        **sin acceso a la casilla**.
        """
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com", verificado=False)
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email=beto.email
        )

        with pytest.raises(InvitationEmailMismatchError, match="Verificá"):
            await servicio.accept_invitation(token=emitida.token, user_id=beto.id)

    @pytest.mark.anyio
    async def test_es_de_un_solo_uso(self, contenedor, servicio):
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email=beto.email
        )
        await servicio.accept_invitation(token=emitida.token, user_id=beto.id)

        with pytest.raises(InvitationError):
            await servicio.accept_invitation(token=emitida.token, user_id=beto.id)

    @pytest.mark.anyio
    async def test_una_invitacion_vencida_no_sirve(self, contenedor, servicio, reloj):
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email=beto.email
        )

        reloj.advance(days=8)  # el TTL por default son 7

        with pytest.raises(InvitationError):
            await servicio.accept_invitation(token=emitida.token, user_id=beto.id)

    @pytest.mark.anyio
    async def test_un_token_inventado_no_sirve(self, contenedor, servicio):
        beto = await _usuario(contenedor, "beto@ejemplo.com")

        with pytest.raises(InvitationError):
            await servicio.accept_invitation(token="inventado", user_id=beto.id)

    @pytest.mark.anyio
    async def test_ocho_aceptaciones_concurrentes_dejan_pasar_una(
        self, contenedor, servicio
    ):
        """
        Es la razón por la que `consume` es una sentencia única. Sin él, las dos crearían una
        membresía y el `UNIQUE` rechazaría la segunda con un error de base en vez de con un
        mensaje.
        """
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email=beto.email
        )

        resultados = await asyncio.gather(
            *(
                servicio.accept_invitation(token=emitida.token, user_id=beto.id)
                for _ in range(8)
            ),
            return_exceptions=True,
        )

        ganaron = [r for r in resultados if not isinstance(r, BaseException)]
        perdieron = [
            r
            for r in resultados
            if isinstance(r, (InvitationError, AlreadyAMemberError))
        ]

        assert len(ganaron) == 1, f"ganaron {len(ganaron)}"
        assert len(perdieron) == 7
        assert len(await servicio.list_members(
            organization_id=org.id, actor_id=dueno.id
        )) == 2


# ── Revocar ───────────────────────────────────────────────────────────────────
class TestRevocar:
    @pytest.mark.anyio
    async def test_revocar_invalida_la_invitacion(self, contenedor, servicio):
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email=beto.email
        )

        await servicio.revoke_invitation(
            organization_id=org.id,
            actor_id=dueno.id,
            invitation_id=emitida.invitation.id,
        )

        with pytest.raises(InvitationError):
            await servicio.accept_invitation(token=emitida.token, user_id=beto.id)

    @pytest.mark.anyio
    async def test_revocada_deja_rastro(self, contenedor, servicio):
        """
        Revocada y no borrada: es información de auditoría — dice que alguien invitó y después se
        arrepintió.
        """
        from sqlalchemy import select

        from hexcore.darwin.plugins.organization.models import InvitationModel
        from hexcore.infrastructure.uow.scopes import session_scope

        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email="beto@ejemplo.com"
        )

        await servicio.revoke_invitation(
            organization_id=org.id,
            actor_id=dueno.id,
            invitation_id=emitida.invitation.id,
        )

        async with session_scope() as sesion:
            fila = (await sesion.execute(select(InvitationModel))).scalar_one()

        assert fila.status == "revoked"
        assert fila.invited_by == dueno.id

    @pytest.mark.anyio
    async def test_revocar_dos_veces_falla(self, contenedor, servicio):
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email="beto@ejemplo.com"
        )
        await servicio.revoke_invitation(
            organization_id=org.id,
            actor_id=dueno.id,
            invitation_id=emitida.invitation.id,
        )

        with pytest.raises(InvitationError, match="ya no está pendiente"):
            await servicio.revoke_invitation(
                organization_id=org.id,
                actor_id=dueno.id,
                invitation_id=emitida.invitation.id,
            )

    @pytest.mark.anyio
    async def test_la_invitacion_de_otra_organizacion_no_se_revoca(
        self, contenedor, servicio
    ):
        """
        Mismo error que "no existe": decir "existe pero no es tuya" confirmaría la existencia de
        invitaciones de otras organizaciones.
        """
        ana = await _usuario(contenedor, "ana@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        de_ana = await servicio.create(name="De Ana", owner_id=ana.id)
        de_beto = await servicio.create(name="De Beto", owner_id=beto.id)
        emitida = await servicio.invite(
            organization_id=de_ana.id, actor_id=ana.id, email="x@ejemplo.com"
        )

        with pytest.raises(InvitationError, match="No se encontró"):
            await servicio.revoke_invitation(
                organization_id=de_beto.id,
                actor_id=beto.id,
                invitation_id=emitida.invitation.id,
            )

    @pytest.mark.anyio
    async def test_las_pendientes_se_listan(self, contenedor, servicio):
        dueno = await _usuario(contenedor, "dueno@ejemplo.com")
        org = await servicio.create(name="Mi Empresa", owner_id=dueno.id)
        await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email="uno@ejemplo.com"
        )
        segunda = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email="dos@ejemplo.com"
        )
        await servicio.revoke_invitation(
            organization_id=org.id,
            actor_id=dueno.id,
            invitation_id=segunda.invitation.id,
        )

        pendientes = await servicio.list_pending_invitations(
            organization_id=org.id, actor_id=dueno.id
        )

        assert [i.email for i in pendientes] == ["uno@ejemplo.com"]


# ── La invariante del último owner ────────────────────────────────────────────
class TestUltimoOwner:
    @pytest.mark.anyio
    async def test_el_unico_owner_no_se_puede_degradar(self, contenedor, servicio):
        """
        ⚠️ La invariante más importante del plugin. Degradar al último `owner` deja la organización
        inadministrable —nadie puede invitar, nadie puede cambiar roles— y salir de ahí requiere un
        `UPDATE` a mano en producción.
        """
        org, dueno, _, _ = await _con_equipo(contenedor, servicio)

        with pytest.raises(LastOwnerError, match="sin ningún owner"):
            await servicio.set_role(
                organization_id=org.id,
                actor_id=dueno.id,
                target_user_id=dueno.id,
                role=OrgRole.ADMIN,
            )

    @pytest.mark.anyio
    async def test_el_unico_owner_no_se_puede_sacar(self, contenedor, servicio):
        org, dueno, _, _ = await _con_equipo(contenedor, servicio)

        with pytest.raises(LastOwnerError):
            await servicio.remove_member(
                organization_id=org.id,
                actor_id=dueno.id,
                target_user_id=dueno.id,
            )

    @pytest.mark.anyio
    async def test_con_dos_owners_uno_se_puede_ir(self, contenedor, servicio):
        org, dueno, admin, _ = await _con_equipo(contenedor, servicio)
        await servicio.set_role(
            organization_id=org.id,
            actor_id=dueno.id,
            target_user_id=admin.id,
            role=OrgRole.OWNER,
        )

        await servicio.remove_member(
            organization_id=org.id, actor_id=dueno.id, target_user_id=dueno.id
        )

        assert await servicio.require_role(
            organization_id=org.id, user_id=admin.id, minimum=OrgRole.OWNER
        )

    @pytest.mark.anyio
    async def test_dos_degradaciones_concurrentes_dejan_un_owner(
        self, contenedor, servicio
    ):
        """
        ⚠️ Es la razón por la que el conteo va **a la base** en cada llamada y no a una lista leída
        antes. Con el conteo en memoria, las dos peticiones ven "hay 2 owners" y las dos degradan —
        y la organización queda sin ninguno.
        """
        org, dueno, admin, _ = await _con_equipo(contenedor, servicio)
        await servicio.set_role(
            organization_id=org.id,
            actor_id=dueno.id,
            target_user_id=admin.id,
            role=OrgRole.OWNER,
        )

        await asyncio.gather(
            servicio.set_role(
                organization_id=org.id,
                actor_id=dueno.id,
                target_user_id=dueno.id,
                role=OrgRole.MEMBER,
            ),
            servicio.set_role(
                organization_id=org.id,
                actor_id=admin.id,
                target_user_id=admin.id,
                role=OrgRole.MEMBER,
            ),
            return_exceptions=True,
        )

        from hexcore.darwin.plugins.organization.repository import (
            SqlAlchemyMemberRepository,
        )

        owners = await SqlAlchemyMemberRepository().count_by_role(
            org.id, OrgRole.OWNER
        )
        assert owners >= 1, "la organización nunca queda sin owner"

    @pytest.mark.anyio
    async def test_borrar_la_organizacion_si_se_permite(self, contenedor, servicio):
        """
        Borrarla entera es distinto de dejarla sin owner: la invariante existe para que no quede
        una organización huérfana, y acá no queda ninguna.
        """
        org, dueno, _, _ = await _con_equipo(contenedor, servicio)

        await servicio.delete(organization_id=org.id, actor_id=dueno.id)

        with pytest.raises(OrganizationNotFoundError):
            await servicio.get(org.id)

    @pytest.mark.anyio
    async def test_un_admin_no_puede_borrar_la_organizacion(
        self, contenedor, servicio
    ):
        org, _, admin, _ = await _con_equipo(contenedor, servicio)

        with pytest.raises(InsufficientOrgRoleError):
            await servicio.delete(organization_id=org.id, actor_id=admin.id)


# ── La jerarquía ──────────────────────────────────────────────────────────────
class TestJerarquia:
    @pytest.mark.anyio
    async def test_un_admin_no_se_asciende_a_owner(self, contenedor, servicio):
        """Si pudiera, el modelo de roles sería decorativo."""
        org, _, admin, _ = await _con_equipo(contenedor, servicio)

        with pytest.raises(InsufficientOrgRoleError, match="No podés asignar"):
            await servicio.set_role(
                organization_id=org.id,
                actor_id=admin.id,
                target_user_id=admin.id,
                role=OrgRole.OWNER,
            )

    @pytest.mark.anyio
    async def test_un_admin_no_degrada_a_otro_admin(self, contenedor, servicio):
        """Un par no administra a un par: si no, el primero que llega se queda solo arriba."""
        org, dueno, admin, _ = await _con_equipo(contenedor, servicio)
        otro = await _usuario(contenedor, "otro-admin@ejemplo.com")
        emitida = await servicio.invite(
            organization_id=org.id,
            actor_id=dueno.id,
            email=otro.email,
            role=OrgRole.ADMIN,
        )
        await servicio.accept_invitation(token=emitida.token, user_id=otro.id)

        with pytest.raises(InsufficientOrgRoleError, match="sólo se administra hacia abajo"):
            await servicio.set_role(
                organization_id=org.id,
                actor_id=admin.id,
                target_user_id=otro.id,
                role=OrgRole.MEMBER,
            )

    @pytest.mark.anyio
    async def test_un_admin_no_degrada_al_owner(self, contenedor, servicio):
        org, dueno, admin, _ = await _con_equipo(contenedor, servicio)

        with pytest.raises(InsufficientOrgRoleError):
            await servicio.set_role(
                organization_id=org.id,
                actor_id=admin.id,
                target_user_id=dueno.id,
                role=OrgRole.MEMBER,
            )

    @pytest.mark.anyio
    async def test_un_admin_si_administra_a_un_member(self, contenedor, servicio):
        org, _, admin, miembro = await _con_equipo(contenedor, servicio)

        actualizado = await servicio.set_role(
            organization_id=org.id,
            actor_id=admin.id,
            target_user_id=miembro.id,
            role=OrgRole.ADMIN,
        )

        assert actualizado.role is OrgRole.ADMIN

    @pytest.mark.anyio
    async def test_un_admin_no_saca_a_otro_admin(self, contenedor, servicio):
        org, dueno, admin, _ = await _con_equipo(contenedor, servicio)
        otro = await _usuario(contenedor, "otro-admin@ejemplo.com")
        emitida = await servicio.invite(
            organization_id=org.id,
            actor_id=dueno.id,
            email=otro.email,
            role=OrgRole.ADMIN,
        )
        await servicio.accept_invitation(token=emitida.token, user_id=otro.id)

        with pytest.raises(InsufficientOrgRoleError):
            await servicio.remove_member(
                organization_id=org.id, actor_id=admin.id, target_user_id=otro.id
            )

    @pytest.mark.anyio
    async def test_cambiar_el_rol_de_un_no_miembro_falla(self, contenedor, servicio):
        org, dueno, _, _ = await _con_equipo(contenedor, servicio)
        ajeno = await _usuario(contenedor, "ajeno@ejemplo.com")

        with pytest.raises(NotAMemberError):
            await servicio.set_role(
                organization_id=org.id,
                actor_id=dueno.id,
                target_user_id=ajeno.id,
                role=OrgRole.ADMIN,
            )


# ── Irse ──────────────────────────────────────────────────────────────────────
class TestIrse:
    @pytest.mark.anyio
    async def test_un_member_se_puede_ir_solo(self, contenedor, servicio):
        """
        Nadie tiene que pedir permiso para dejar de trabajar en un lugar: irse no requiere ningún
        rol.
        """
        org, dueno, _, miembro = await _con_equipo(contenedor, servicio)

        await servicio.remove_member(
            organization_id=org.id, actor_id=miembro.id, target_user_id=miembro.id
        )

        with pytest.raises(NotAMemberError):
            await servicio.require_role(
                organization_id=org.id, user_id=miembro.id, minimum=OrgRole.MEMBER
            )
        assert len(
            await servicio.list_members(organization_id=org.id, actor_id=dueno.id)
        ) == 2

    @pytest.mark.anyio
    async def test_un_no_miembro_no_se_puede_ir(self, contenedor, servicio):
        org, *_ = await _con_equipo(contenedor, servicio)
        ajeno = await _usuario(contenedor, "ajeno@ejemplo.com")

        with pytest.raises(NotAMemberError):
            await servicio.remove_member(
                organization_id=org.id, actor_id=ajeno.id, target_user_id=ajeno.id
            )

    @pytest.mark.anyio
    async def test_un_member_no_saca_a_otro(self, contenedor, servicio):
        org, _, admin, miembro = await _con_equipo(contenedor, servicio)

        with pytest.raises(InsufficientOrgRoleError):
            await servicio.remove_member(
                organization_id=org.id,
                actor_id=miembro.id,
                target_user_id=admin.id,
            )

    @pytest.mark.anyio
    async def test_sacar_y_volver_a_invitar_funciona(self, contenedor, servicio):
        org, dueno, _, miembro = await _con_equipo(contenedor, servicio)
        await servicio.remove_member(
            organization_id=org.id, actor_id=dueno.id, target_user_id=miembro.id
        )

        emitida = await servicio.invite(
            organization_id=org.id, actor_id=dueno.id, email=miembro.email
        )
        vuelto = await servicio.accept_invitation(
            token=emitida.token, user_id=miembro.id
        )

        assert vuelto.organization_id == org.id


# ── Varias organizaciones ─────────────────────────────────────────────────────
class TestVariasOrganizaciones:
    @pytest.mark.anyio
    async def test_alguien_puede_estar_en_varias_con_roles_distintos(
        self, contenedor, servicio
    ):
        """Es el punto del multi-tenancy: el rol es por organización, no por persona."""
        ana = await _usuario(contenedor, "ana@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com")

        de_ana = await servicio.create(name="De Ana", owner_id=ana.id)
        de_beto = await servicio.create(name="De Beto", owner_id=beto.id)

        emitida = await servicio.invite(
            organization_id=de_ana.id,
            actor_id=ana.id,
            email=beto.email,
            role=OrgRole.MEMBER,
        )
        await servicio.accept_invitation(token=emitida.token, user_id=beto.id)

        assert (
            await servicio.require_role(
                organization_id=de_beto.id, user_id=beto.id, minimum=OrgRole.OWNER
            )
        ).role is OrgRole.OWNER
        with pytest.raises(InsufficientOrgRoleError):
            await servicio.require_role(
                organization_id=de_ana.id, user_id=beto.id, minimum=OrgRole.ADMIN
            )

    @pytest.mark.anyio
    async def test_las_membresias_se_listan_por_usuario(self, contenedor, servicio):
        ana = await _usuario(contenedor, "ana@ejemplo.com")
        await servicio.create(name="Una", owner_id=ana.id, slug="una")
        await servicio.create(name="Otra", owner_id=ana.id, slug="otra")

        membresias = await servicio.list_for_user(ana.id)

        assert len(membresias) == 2
        assert all(m.role is OrgRole.OWNER for m in membresias)


# ── El plugin como plugin ─────────────────────────────────────────────────────
class TestPlugin:
    def test_aporta_sus_tres_mixins(self, plugin):
        from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

        mixins = plugin.tables()

        assert sorted(mixins) == [
            "InvitationMixin",
            "MemberMixin",
            "OrganizationMixin",
        ]
        for mixin in mixins.values():
            assert not issubclass(mixin, Base)

    def test_aporta_su_mapa_de_excepciones(self, plugin):
        mapa = plugin.exception_status_map()

        assert mapa[NotAMemberError] == 403
        assert mapa[LastOwnerError] == 409
        assert mapa[OrganizationNotFoundError] == 404
        assert mapa[InvitationEmailMismatchError] == 403

    def test_no_mapea_la_excepcion_base(self, plugin):
        assert OrganizationError not in plugin.exception_status_map()

    def test_el_servicio_sin_registrar_falla_con_remediacion(self, reloj):
        reset_identity()
        configure_identity(
            IdentityConfig(secret_key=CLAVE),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        )
        try:
            with pytest.raises(RuntimeError) as excinfo:
                get_organization_service()

            assert "OrganizationPlugin" in str(excinfo.value)
        finally:
            reset_identity()

    def test_los_cinco_plugins_conviven(self):
        """El registro completo de la Fase 9, con sus ocho mixins."""
        from hexcore.darwin.plugins.impersonate import ImpersonatePlugin
        from hexcore.darwin.plugins.magic_link import MagicLinkPlugin
        from hexcore.darwin.plugins.oauth import OAuthPlugin
        from hexcore.darwin.plugins.passkey import PasskeyPlugin
        from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

        registro = PluginRegistry(
            [
                OrganizationPlugin(),
                ImpersonatePlugin(),
                PasskeyPlugin(rp_id="mi-app.test", origins=["https://mi-app.test"]),
                OAuthPlugin(),
                TwoFactorPlugin(),
                MagicLinkPlugin(),
            ]
        )
        registro.validate()

        assert registro.names == (
            "two_factor",
            "oauth",
            "passkey",
            "magic_link",
            "impersonate",
            "organization",
        )
        # 1 de two_factor + 1 de oauth + 2 de passkey + 3 de organization. `magic_link` e
        # `impersonate` no aportan tabla: reusan las del núcleo.
        assert len(registro.tables()) == 7
        assert len(registro.exception_status_map()) == 29


# ── El borde HTTP ─────────────────────────────────────────────────────────────
@pytest.fixture
def cliente_http(contenedor, plugin):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hexcore.darwin import build_identity_router
    from hexcore.fastapi import AppFeatures, create_app

    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True, health=False),
        routers=[build_identity_router(), *plugin.routers()],
    )
    with TestClient(app) as http:
        yield http


async def _bearer(contenedor, email):
    _, _, par = await contenedor.identity_service().sign_in(
        email=email, password=PASS, transport="bearer"
    )
    return {
        "Authorization": f"Bearer {par.access_token}",
        "X-Darwin-Transport": "bearer",
    }


class TestHttp:
    @pytest.mark.anyio
    async def test_el_flujo_completo(self, contenedor, cliente_http):
        await _usuario(contenedor, "dueno@ejemplo.com")
        beto = await _usuario(contenedor, "beto@ejemplo.com")
        dueno_auth = await _bearer(contenedor, "dueno@ejemplo.com")
        beto_auth = await _bearer(contenedor, "beto@ejemplo.com")

        creada = cliente_http.post(
            "/organizations",
            json={"name": "Mi Empresa", "metadata": {"plan": "pro"}},
            headers=dueno_auth,
        )
        assert creada.status_code == 201, creada.text
        org_id = creada.json()["id"]
        assert creada.json()["slug"] == "mi-empresa"

        invitacion = cliente_http.post(
            f"/organizations/{org_id}/invitations",
            json={"email": beto.email, "role": "admin"},
            headers=dueno_auth,
        )
        assert invitacion.status_code == 201, invitacion.text
        token = invitacion.json()["token"]

        aceptada = cliente_http.post(
            "/organizations/invitations/accept",
            json={"token": token},
            headers=beto_auth,
        )
        assert aceptada.status_code == 200, aceptada.text
        assert aceptada.json()["role"] == "admin"

        miembros = cliente_http.get(
            f"/organizations/{org_id}/members", headers=beto_auth
        )
        assert len(miembros.json()) == 2

        mias = cliente_http.get("/organizations", headers=beto_auth)
        assert len(mias.json()) == 1

    @pytest.mark.anyio
    async def test_la_ruta_de_aceptar_no_choca_con_la_parametrica(
        self, contenedor, cliente_http
    ):
        """
        `POST /organizations/invitations/accept` y
        `POST /organizations/{organization_id}/invitations` tienen la misma cantidad de segmentos.
        FastAPI resuelve por orden de registro, así que el test fija que la de aceptar llegue a su
        handler y no intente parsear `"invitations"` como UUID.
        """
        await _usuario(contenedor, "ana@ejemplo.com")
        auth = await _bearer(contenedor, "ana@ejemplo.com")

        respuesta = cliente_http.post(
            "/organizations/invitations/accept",
            json={"token": "inventado"},
            headers=auth,
        )

        assert respuesta.status_code == 401, respuesta.text

    @pytest.mark.anyio
    async def test_sin_sesion_da_401(self, contenedor, cliente_http):
        assert cliente_http.get("/organizations").status_code == 401
        assert (
            cliente_http.post("/organizations", json={"name": "X"}).status_code == 401
        )

    @pytest.mark.anyio
    async def test_un_no_miembro_da_403(self, contenedor, cliente_http):
        await _usuario(contenedor, "dueno@ejemplo.com")
        await _usuario(contenedor, "ajeno@ejemplo.com")
        dueno_auth = await _bearer(contenedor, "dueno@ejemplo.com")
        ajeno_auth = await _bearer(contenedor, "ajeno@ejemplo.com")

        org_id = cliente_http.post(
            "/organizations", json={"name": "Mi Empresa"}, headers=dueno_auth
        ).json()["id"]

        respuesta = cliente_http.get(
            f"/organizations/{org_id}/members", headers=ajeno_auth
        )

        assert respuesta.status_code == 403, respuesta.text

    @pytest.mark.anyio
    async def test_el_slug_repetido_da_409(self, contenedor, cliente_http):
        """El status del plugin, llegando al borde por `exception_status_map()`."""
        await _usuario(contenedor, "ana@ejemplo.com")
        auth = await _bearer(contenedor, "ana@ejemplo.com")
        cliente_http.post(
            "/organizations", json={"name": "Mi Empresa"}, headers=auth
        )

        respuesta = cliente_http.post(
            "/organizations", json={"name": "Mi Empresa"}, headers=auth
        )

        assert respuesta.status_code == 409, respuesta.text

    @pytest.mark.anyio
    async def test_degradar_al_ultimo_owner_da_409(self, contenedor, cliente_http):
        await _usuario(contenedor, "ana@ejemplo.com")
        auth = await _bearer(contenedor, "ana@ejemplo.com")
        creada = cliente_http.post(
            "/organizations", json={"name": "Mi Empresa"}, headers=auth
        ).json()
        yo = cliente_http.get("/organizations", headers=auth).json()[0]["user_id"]

        respuesta = cliente_http.patch(
            f"/organizations/{creada['id']}/members/{yo}",
            json={"role": "member"},
            headers=auth,
        )

        assert respuesta.status_code == 409, respuesta.text

    @pytest.mark.anyio
    async def test_las_invitaciones_pendientes_no_traen_el_token(
        self, contenedor, cliente_http
    ):
        """
        El token existe una sola vez, en la respuesta de invitar; el hash no le sirve a nadie del
        otro lado.
        """
        await _usuario(contenedor, "ana@ejemplo.com")
        auth = await _bearer(contenedor, "ana@ejemplo.com")
        org_id = cliente_http.post(
            "/organizations", json={"name": "Mi Empresa"}, headers=auth
        ).json()["id"]
        cliente_http.post(
            f"/organizations/{org_id}/invitations",
            json={"email": "beto@ejemplo.com"},
            headers=auth,
        )

        pendientes = cliente_http.get(
            f"/organizations/{org_id}/invitations", headers=auth
        ).json()

        assert len(pendientes) == 1
        assert set(pendientes[0]) == {"id", "email", "role", "expires_at"}

    @pytest.mark.anyio
    async def test_el_patch_no_acepta_slug(self, contenedor, cliente_http):
        """
        Un slug que cambia rompe cada link guardado. El campo no está en el cuerpo, así que
        mandarlo se ignora — y el slug queda igual.
        """
        await _usuario(contenedor, "ana@ejemplo.com")
        auth = await _bearer(contenedor, "ana@ejemplo.com")
        creada = cliente_http.post(
            "/organizations", json={"name": "Mi Empresa"}, headers=auth
        ).json()

        actualizada = cliente_http.patch(
            f"/organizations/{creada['id']}",
            json={"name": "Otro Nombre", "slug": "otro-slug"},
            headers=auth,
        )

        assert actualizada.status_code == 200, actualizada.text
        assert actualizada.json()["slug"] == "mi-empresa"
        assert actualizada.json()["name"] == "Otro Nombre"
