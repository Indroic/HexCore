"""
El router de `organization`. Requiere `[api]`.

Nueve rutas, **todas autenticadas**. No hay ninguna pública: hasta aceptar una invitación exige
sesión, porque la invitación está atada a un mail y sin cuenta no hay mail que comparar.

⚠️ **El `actor_id` sale siempre del contexto**, nunca del cuerpo ni de la query. Es lo que hace que
la jerarquía de roles signifique algo: un `actor_id` que el cliente rellena es un `actor_id` que el
cliente puede mentir, y mentirlo acá es administrar una organización en nombre de su owner.
"""
# pyright: reportUnusedFunction=false
#
# En un módulo de router ninguna función se llama por nombre: a todas las registra su decorador.
from __future__ import annotations

import typing as t
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from hexcore.darwin.plugins.organization.domain import OrgRole

__all__ = [
    "CreateOrganizationBody",
    "UpdateOrganizationBody",
    "InviteBody",
    "AcceptInvitationBody",
    "SetRoleBody",
    "OrganizationOut",
    "MemberOut",
    "InvitationOut",
    "build_organization_router",
]


class CreateOrganizationBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    #: Opcional: si no viene, se deriva del nombre. **No se puede cambiar después** — ver
    #: `Organization`.
    slug: str | None = Field(default=None, max_length=128)
    metadata: dict[str, t.Any] = Field(default_factory=dict)


class UpdateOrganizationBody(BaseModel):
    """
    Nombre y metadata. **El slug no está**, y no es un olvido: cambiarlo rompe cada link guardado,
    cada bookmark y cada integración que lo tenga fijo.
    """

    name: str = Field(min_length=1, max_length=255)
    metadata: dict[str, t.Any] = Field(default_factory=dict)


class InviteBody(BaseModel):
    email: str
    role: OrgRole = OrgRole.MEMBER


class AcceptInvitationBody(BaseModel):
    token: str


class SetRoleBody(BaseModel):
    role: OrgRole


class OrganizationOut(BaseModel):
    id: str
    name: str
    slug: str
    metadata: dict[str, t.Any]


class MemberOut(BaseModel):
    user_id: str
    role: OrgRole
    created_at: str | None = None


class InvitationOut(BaseModel):
    """
    Una invitación pendiente, para la pantalla de administración.

    **No lleva el token ni su hash.** El token existe una sola vez, en la respuesta de invitar; el
    hash no le sirve a nadie del otro lado y devolverlo sería superficie gratis.
    """

    id: str
    email: str
    role: OrgRole
    expires_at: str


def build_organization_router(
    *,
    prefix: str = "/organizations",
    tags: t.Sequence[str] = ("organizations",),
) -> APIRouter:
    """
    Construye el router del plugin.

    Args:
        prefix: Prefijo de las rutas. `/organizations` y no `/auth/...`: son recursos del producto,
            no del flujo de autenticación.
        tags: Tags de OpenAPI.

    Uso::

        from hexcore.darwin.plugins.organization.router import build_organization_router

        app = create_app(routers=[build_identity_router(), build_organization_router()])
    """
    from hexcore.darwin.infrastructure.api.dependencies import provide_auth

    router = APIRouter(prefix=prefix, tags=list(tags))

    # ── La organización ───────────────────────────────────────────────────────
    @router.post("", status_code=201)
    async def crear(
        payload: CreateOrganizationBody, auth: t.Any = Depends(provide_auth)
    ) -> OrganizationOut:
        """Crea una organización y hace `owner` al actor."""
        from hexcore.darwin.plugins.organization import get_organization_service

        organizacion = await get_organization_service().create(
            name=payload.name,
            owner_id=auth.actor_id,
            slug=payload.slug,
            metadata=payload.metadata,
        )
        return _org_out(organizacion)

    @router.get("")
    async def mias(auth: t.Any = Depends(provide_auth)) -> list[MemberOut]:
        """
        Las membresías del actor, para el selector de organización.

        Devuelve membresías y no organizaciones porque el rol es la mitad de la información: la
        interfaz necesita saber si mostrar el botón de administrar.
        """
        from hexcore.darwin.plugins.organization import get_organization_service

        miembros = await get_organization_service().list_for_user(auth.actor_id)
        return [_member_out(m) for m in miembros]

    @router.get("/{organization_id}")
    async def ver(
        organization_id: UUID, auth: t.Any = Depends(provide_auth)
    ) -> OrganizationOut:
        """La organización. Exige ser miembro."""
        from hexcore.darwin.plugins.organization import get_organization_service
        from hexcore.darwin.plugins.organization.domain import OrgRole as Rol

        servicio = get_organization_service()
        await servicio.require_role(
            organization_id=organization_id,
            user_id=auth.actor_id,
            minimum=Rol.MEMBER,
        )
        return _org_out(await servicio.get(organization_id))

    @router.patch("/{organization_id}")
    async def actualizar(
        organization_id: UUID,
        payload: UpdateOrganizationBody,
        auth: t.Any = Depends(provide_auth),
    ) -> OrganizationOut:
        """Actualiza nombre y metadata. Requiere ser `admin` o más."""
        from hexcore.darwin.plugins.organization import get_organization_service

        actualizada = await get_organization_service().update(
            organization_id=organization_id,
            actor_id=auth.actor_id,
            name=payload.name,
            metadata=payload.metadata,
        )
        return _org_out(actualizada)

    @router.delete("/{organization_id}")
    async def borrar(
        organization_id: UUID, auth: t.Any = Depends(provide_auth)
    ) -> dict[str, bool]:
        """Borra la organización. **Sólo un `owner`.**"""
        from hexcore.darwin.plugins.organization import get_organization_service

        await get_organization_service().delete(
            organization_id=organization_id, actor_id=auth.actor_id
        )
        return {"deleted": True}

    # ── Los miembros ──────────────────────────────────────────────────────────
    @router.get("/{organization_id}/members")
    async def miembros(
        organization_id: UUID, auth: t.Any = Depends(provide_auth)
    ) -> list[MemberOut]:
        """Los miembros. Exige ser miembro — ver el docstring del servicio."""
        from hexcore.darwin.plugins.organization import get_organization_service

        lista = await get_organization_service().list_members(
            organization_id=organization_id, actor_id=auth.actor_id
        )
        return [_member_out(m) for m in lista]

    @router.patch("/{organization_id}/members/{user_id}")
    async def cambiar_rol(
        organization_id: UUID,
        user_id: UUID,
        payload: SetRoleBody,
        auth: t.Any = Depends(provide_auth),
    ) -> MemberOut:
        """Cambia el rol de alguien. Requiere `admin` y estar por encima — ver el servicio."""
        from hexcore.darwin.plugins.organization import get_organization_service

        miembro = await get_organization_service().set_role(
            organization_id=organization_id,
            actor_id=auth.actor_id,
            target_user_id=user_id,
            role=payload.role,
        )
        return _member_out(miembro)

    @router.delete("/{organization_id}/members/{user_id}")
    async def sacar(
        organization_id: UUID, user_id: UUID, auth: t.Any = Depends(provide_auth)
    ) -> dict[str, bool]:
        """
        Saca a alguien.

        **Irse uno mismo siempre se permite** —salvo siendo el último `owner`— y no requiere
        ningún rol: nadie tiene que pedir permiso para dejar de trabajar en un lugar.
        """
        from hexcore.darwin.plugins.organization import get_organization_service

        await get_organization_service().remove_member(
            organization_id=organization_id,
            actor_id=auth.actor_id,
            target_user_id=user_id,
        )
        return {"removed": True}

    # ── Las invitaciones ──────────────────────────────────────────────────────
    @router.post("/{organization_id}/invitations", status_code=201)
    async def invitar(
        organization_id: UUID,
        payload: InviteBody,
        auth: t.Any = Depends(provide_auth),
    ) -> dict[str, t.Any]:
        """
        Invita a alguien. Requiere ser `admin` o más, y no se puede invitar por encima del rol
        propio.

        **Devuelve el token en el cuerpo** por la misma razón que `/auth/sign-up` devuelve el
        código de verificación: el framework no manda mails. ⚠️ En producción armá el link con este
        valor, mandalo por mail, y respondé sólo `{"sent": true}`.
        """
        from hexcore.darwin.plugins.organization import get_organization_service

        emitida = await get_organization_service().invite(
            organization_id=organization_id,
            actor_id=auth.actor_id,
            email=payload.email,
            role=payload.role,
        )
        return {
            "invitation": _invitation_out(emitida.invitation).model_dump(),
            "token": emitida.token,
        }

    @router.get("/{organization_id}/invitations")
    async def pendientes(
        organization_id: UUID, auth: t.Any = Depends(provide_auth)
    ) -> list[InvitationOut]:
        """Las invitaciones pendientes. Requiere ser `admin` o más."""
        from hexcore.darwin.plugins.organization import get_organization_service

        lista = await get_organization_service().list_pending_invitations(
            organization_id=organization_id, actor_id=auth.actor_id
        )
        return [_invitation_out(i) for i in lista]

    @router.delete("/{organization_id}/invitations/{invitation_id}")
    async def revocar(
        organization_id: UUID,
        invitation_id: UUID,
        auth: t.Any = Depends(provide_auth),
    ) -> dict[str, bool]:
        """Revoca una invitación pendiente. Requiere ser `admin` o más."""
        from hexcore.darwin.plugins.organization import get_organization_service

        await get_organization_service().revoke_invitation(
            organization_id=organization_id,
            actor_id=auth.actor_id,
            invitation_id=invitation_id,
        )
        return {"revoked": True}

    @router.post("/invitations/accept")
    async def aceptar(
        payload: AcceptInvitationBody, auth: t.Any = Depends(provide_auth)
    ) -> MemberOut:
        """
        Acepta una invitación.

        **Exige sesión**, y por eso no es pública: la invitación está atada a un mail, y sin cuenta
        no hay mail que comparar. **No se permite estando impersonado**: aceptar una invitación en
        nombre de la persona que estás impersonando la mete en una organización sin que se enterara.
        """
        from hexcore.darwin.domain.exceptions import ImpersonationNotPermittedError
        from hexcore.darwin.plugins.organization import get_organization_service

        if auth.is_impersonating:
            raise ImpersonationNotPermittedError(
                "No se puede aceptar una invitación en nombre de otra persona mientras la "
                "impersonás."
            )

        miembro = await get_organization_service().accept_invitation(
            token=payload.token, user_id=auth.actor_id
        )
        return _member_out(miembro)

    return router


def _org_out(organizacion: t.Any) -> OrganizationOut:
    return OrganizationOut(
        id=str(organizacion.id),
        name=organizacion.name,
        slug=organizacion.slug,
        metadata=dict(organizacion.metadata),
    )


def _member_out(miembro: t.Any) -> MemberOut:
    return MemberOut(
        user_id=str(miembro.user_id),
        role=miembro.role,
        created_at=(
            miembro.created_at.isoformat() if miembro.created_at else None
        ),
    )


def _invitation_out(invitacion: t.Any) -> InvitationOut:
    return InvitationOut(
        id=str(invitacion.id),
        email=invitacion.email,
        role=invitacion.role,
        expires_at=invitacion.expires_at.isoformat(),
    )
