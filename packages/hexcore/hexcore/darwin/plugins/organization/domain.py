"""
El dominio de `organization`: las entidades, los roles, los puertos y las excepciones.

**El modelo es una jerarquía de tres roles y nada más**: `owner` > `admin` > `member`. No hay un
sistema de permisos por organización, y eso es deliberado: HexCore ya tiene `RoleRegistry` para
que el consumidor declare su modelo de autorización, y un segundo sistema de permisos adentro del
plugin le daría dos lugares donde mirar cuando algo no autoriza. Lo que el plugin garantiza es
quién puede administrar a quién; qué puede hacer un `member` en tu producto es tuyo.

Las dos invariantes que el plugin no deja romper, y las dos son bugs clásicos:

1. **Una organización nunca queda sin `owner`.** Sacar o degradar al último la vuelve
   inadministrable: nadie puede invitar, nadie puede cambiar roles, y sólo se sale con un `UPDATE`
   a mano en producción.
2. **Nadie asciende a alguien por encima de sí mismo, ni actúa sobre un par o un superior.** Sin
   eso, un `admin` se hace `owner` y el modelo de roles es decorativo.
"""
from __future__ import annotations

import abc
import enum
import typing as t
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from hexcore.darwin.domain.exceptions import AuthorizationError, IdentityError

__all__ = [
    "OrgRole",
    "Organization",
    "Member",
    "Invitation",
    "InvitationStatus",
    "AbstractOrganizationRepository",
    "AbstractMemberRepository",
    "AbstractInvitationRepository",
    "OrganizationError",
    "OrganizationNotFoundError",
    "SlugAlreadyTakenError",
    "NotAMemberError",
    "InsufficientOrgRoleError",
    "LastOwnerError",
    "AlreadyAMemberError",
    "InvitationError",
    "InvitationEmailMismatchError",
    "ORGANIZATION_EXCEPTION_STATUS_MAP",
]


class OrgRole(enum.StrEnum):
    """
    El rol de alguien dentro de una organización.

    Sólo tres, y ordenados. Más roles se resuelven con `RoleRegistry` del lado del consumidor: acá
    el orden es lo único que se necesita, porque es lo que decide quién administra a quién.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

    @property
    def rank(self) -> int:
        """
        El nivel, donde **más alto es más poder**.

        Existe como propiedad y no como comparación de strings porque el orden alfabético de
        `admin` < `member` < `owner` es exactamente el equivocado, y un `>` sobre los nombres
        pasaría los tests por casualidad en algunos pares.
        """
        return {OrgRole.MEMBER: 0, OrgRole.ADMIN: 1, OrgRole.OWNER: 2}[self]

    def outranks(self, otro: "OrgRole") -> bool:
        """Si este rol está **estrictamente** por encima del otro."""
        return self.rank > otro.rank

    def at_least(self, otro: "OrgRole") -> bool:
        """Si este rol alcanza el nivel del otro."""
        return self.rank >= otro.rank


class InvitationStatus(enum.StrEnum):
    """
    El estado de una invitación.

    `revoked` existe aparte de borrar la fila porque una invitación revocada es información de
    auditoría: dice que alguien invitó a alguien y después se arrepintió.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"


# ── Las entidades ─────────────────────────────────────────────────────────────
class Organization(BaseModel):
    """
    Una organización.

    `slug` es único y es lo que va en la URL. Se valida al crear y no se puede cambiar por el
    endpoint de actualización: un slug que cambia rompe cada link guardado, cada bookmark y cada
    integración que lo tenga hardcodeado.
    """

    id: UUID
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=128)
    metadata: dict[str, t.Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class Member(BaseModel):
    """Alguien dentro de una organización, con su rol."""

    id: UUID
    organization_id: UUID
    user_id: UUID
    role: OrgRole = OrgRole.MEMBER
    created_at: datetime | None = None


class Invitation(BaseModel):
    """
    Una invitación pendiente.

    **Está atada al mail**, y aceptarla exige que la cuenta que la acepta tenga ese mail. Sin eso,
    reenviar el link de invitación —que es lo que la gente hace— deja entrar a cualquiera con el
    rol que se le había dado a otro.
    """

    id: UUID
    organization_id: UUID
    email: str
    role: OrgRole = OrgRole.MEMBER
    invited_by: UUID
    #: **SHA-256 del token, nunca el token.** El link viaja por mail y queda en el buzón, en los
    #: logs del proveedor y en el historial del cliente: un dump de la tabla no debería sumar la
    #: capacidad de entrar a una organización ajena. El token en claro existe una sola vez, en el
    #: valor que devuelve `invite()`.
    token_hash: str
    status: InvitationStatus = InvitationStatus.PENDING
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime | None = None


# ── Los puertos ───────────────────────────────────────────────────────────────
class AbstractOrganizationRepository(abc.ABC):
    """Las organizaciones."""

    @abc.abstractmethod
    async def add(self, organization: Organization) -> Organization:
        """Crea la organización."""

    @abc.abstractmethod
    async def get(self, organization_id: UUID) -> Organization | None:
        """Por id."""

    @abc.abstractmethod
    async def get_by_slug(self, slug: str) -> Organization | None:
        """Por slug, que es lo que viene en la URL."""

    @abc.abstractmethod
    async def update(self, organization: Organization) -> Organization:
        """Actualiza nombre y metadata. El slug no se toca acá — ver `Organization`."""

    @abc.abstractmethod
    async def delete(self, organization_id: UUID) -> bool:
        """Borra la organización. Las membresías e invitaciones se van por `CASCADE`."""


class AbstractMemberRepository(abc.ABC):
    """Las membresías."""

    @abc.abstractmethod
    async def add(self, member: Member) -> Member:
        """Suma a alguien."""

    @abc.abstractmethod
    async def get(self, organization_id: UUID, user_id: UUID) -> Member | None:
        """La membresía de alguien en una organización. Es la consulta de autorización."""

    @abc.abstractmethod
    async def list_for_organization(self, organization_id: UUID) -> list[Member]:
        """Todos los miembros."""

    @abc.abstractmethod
    async def list_for_user(self, user_id: UUID) -> list[Member]:
        """Todas las organizaciones de alguien, para el selector de la interfaz."""

    @abc.abstractmethod
    async def count_by_role(self, organization_id: UUID, role: OrgRole) -> int:
        """
        Cuántos hay con ese rol. Informativo: para la interfaz y para los mensajes de error.

        ⚠️ **No alcanza para decidir si se puede degradar al último `owner`.** Contar y después
        actualizar es check-then-act: entre las dos sentencias, otra petición hace su propio
        conteo, ve el mismo número y también actualiza. Para eso están
        `set_role(keep_last_owner=True)` y `remove(keep_last_owner=True)`, que resuelven la
        condición **en el `WHERE`**.
        """

    @abc.abstractmethod
    async def set_role(
        self,
        organization_id: UUID,
        user_id: UUID,
        *,
        role: OrgRole,
        keep_last_owner: bool = False,
    ) -> Member | None:
        """
        Cambia el rol. `None` si no era miembro **o si el guardián lo bloqueó**.

        Con `keep_last_owner=True` la sentencia sólo aplica si, después del cambio, queda al menos
        un `owner`. La condición va adentro del `UPDATE` como subconsulta correlacionada: es la
        única forma de que dos degradaciones concurrentes no dejen a la organización sin ninguno.
        """

    @abc.abstractmethod
    async def remove(
        self, organization_id: UUID, user_id: UUID, *, keep_last_owner: bool = False
    ) -> bool:
        """
        Saca a alguien. `False` si no estaba **o si el guardián lo bloqueó**. Ver `set_role`.
        """


class AbstractInvitationRepository(abc.ABC):
    """Las invitaciones."""

    @abc.abstractmethod
    async def add(self, invitation: Invitation) -> Invitation:
        """Crea la invitación."""

    @abc.abstractmethod
    async def get(self, invitation_id: UUID) -> Invitation | None:
        """Por id, para revocarla."""

    @abc.abstractmethod
    async def list_pending(self, organization_id: UUID) -> list[Invitation]:
        """Las pendientes de una organización."""

    @abc.abstractmethod
    async def get_by_token_hash(self, token_hash: str) -> Invitation | None:
        """
        La invitación por el hash de su token, **sin consumirla**.

        Existe porque aceptar una invitación es el único flujo donde no se sabe de qué
        organización es hasta leerla: el token es lo único que trae el invitado. Y se lee antes de
        consumir para poder chequear el mail primero — consumirla y rechazar después la gastaría,
        y el invitado legítimo tendría que pedir otra por un intento que no era suyo.
        """

    @abc.abstractmethod
    async def consume(self, token_hash: str, *, at: datetime) -> Invitation | None:
        """
        Canjea la invitación, en **una sola sentencia**.

        `None` si no existe, venció, ya se usó o fue revocada — un solo valor para los cuatro.
        Atómico por el mismo motivo que el resto: dos aceptaciones concurrentes del mismo link
        crearían dos membresías, y el `UNIQUE` las rechazaría con un error de base en vez de con un
        mensaje.
        """

    @abc.abstractmethod
    async def revoke(self, invitation_id: UUID, *, at: datetime) -> bool:
        """Marca la invitación revocada. `True` si estaba pendiente."""


# ── Las excepciones ───────────────────────────────────────────────────────────
class OrganizationError(IdentityError):
    """Base de las fallas del plugin."""


class OrganizationNotFoundError(OrganizationError):
    """No existe esa organización. 404."""


class SlugAlreadyTakenError(OrganizationError):
    """Ese slug ya está usado. 409."""


class NotAMemberError(AuthorizationError):
    """
    Quien pide no es miembro de la organización. **403, no 404.**

    Un 404 escondería la existencia de la organización, y eso suena mejor de lo que es: los slugs
    son públicos por diseño —van en la URL— así que el 404 no oculta nada y en cambio deja al
    miembro legítimo que perdió su membresía sin entender qué pasó.
    """


class InsufficientOrgRoleError(AuthorizationError):
    """
    El rol no alcanza para esa operación. 403.

    Cubre los tres casos de la jerarquía: no llegar al nivel pedido, intentar ascender a alguien
    por encima de uno mismo, y actuar sobre un par o un superior.
    """


class LastOwnerError(OrganizationError):
    """
    La operación dejaría la organización sin ningún `owner`. 409.

    ⚠️ Es el bug clásico de todo modelo de organizaciones: sacar o degradar al último `owner` la
    vuelve **inadministrable** —nadie puede invitar, nadie puede cambiar roles— y salir de ahí
    requiere un `UPDATE` a mano en producción.
    """


class AlreadyAMemberError(OrganizationError):
    """Ya es miembro. 409."""


class InvitationError(OrganizationError):
    """La invitación no existe, venció, ya se usó o fue revocada. 401."""


class InvitationEmailMismatchError(AuthorizationError):
    """
    La invitación era para otro mail. 403.

    ⚠️ Es el chequeo que evita el abuso más común del flujo: reenviar el link de invitación. Sin
    él, cualquiera a quien le llegue el mail entra con el rol que se le había dado a otro.
    """


#: El mapa que el plugin aporta vía `exception_status_map()`.
ORGANIZATION_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    OrganizationNotFoundError: 404,
    SlugAlreadyTakenError: 409,
    NotAMemberError: 403,
    InsufficientOrgRoleError: 403,
    LastOwnerError: 409,
    AlreadyAMemberError: 409,
    InvitationError: 401,
    InvitationEmailMismatchError: 403,
    # `OrganizationError` (la base) **no** se mapea, por lo mismo que el núcleo no mapea
    # `IdentityError`: `_specificity` ordena por profundidad de MRO, así que mapearla haría que
    # una falla nueva se tragara con ese status en vez de aparecer como un 500 en los tests.
}
