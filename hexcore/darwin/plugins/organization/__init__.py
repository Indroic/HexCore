"""
`organization`: multi-tenancy con roles, invitaciones y las dos invariantes que casi nadie sostiene.

Tres tablas —`organization`, `member`, `invitation`— y una jerarquía de tres roles: `owner` >
`admin` > `member`. **No hay un sistema de permisos por organización**, y eso es deliberado:
HexCore ya tiene `RoleRegistry` para que el consumidor declare su modelo de autorización, y un
segundo sistema adentro del plugin le daría dos lugares donde mirar cuando algo no autoriza. Lo que
el plugin garantiza es **quién puede administrar a quién**; qué puede hacer un `member` en tu
producto es tuyo.

Las tres invariantes, y las tres son bugs clásicos:

1. ⚠️ **Una organización nunca queda sin `owner`.** Sacar o degradar al último la vuelve
   inadministrable, y salir de ahí requiere un `UPDATE` a mano en producción. Se cuenta en la base:
   con dos peticiones concurrentes que degradan a los dos últimos, un conteo en memoria deja a la
   organización sin ninguno.
2. ⚠️ **Nadie asciende a alguien por encima de sí mismo, ni actúa sobre un par o un superior.** Sin
   eso, un `admin` se invita un cómplice como `owner` y el modelo de roles es decorativo.
3. ⚠️ **La invitación está atada al mail, y el mail tiene que estar verificado.** Reenviar el link
   es exactamente lo que la gente hace; sin el chequeo, quien lo reciba entra con el rol de otro. Y
   sin exigir la verificación, alguien registra una cuenta con el mail del invitado y le roba la
   invitación sin acceso a la casilla.

Requiere los extras `[darwin]` y `[api]`. No agrega dependencias.

Uso::

    from hexcore.darwin import PluginRegistry, configure_identity
    from hexcore.darwin.plugins.organization import OrganizationPlugin

    plugins = PluginRegistry([OrganizationPlugin()])
    configure_identity(config, plugins=plugins)

    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True),
        routers=[build_identity_router(), *plugins.routers()],
    )
"""
from __future__ import annotations

import threading
import typing as t
from datetime import timedelta

from hexcore.darwin.domain.plugins import DarwinPlugin
from hexcore.darwin.plugins.organization.domain import (
    ORGANIZATION_EXCEPTION_STATUS_MAP,
    AbstractInvitationRepository,
    AbstractMemberRepository,
    AbstractOrganizationRepository,
    AlreadyAMemberError,
    InsufficientOrgRoleError,
    Invitation,
    InvitationEmailMismatchError,
    InvitationError,
    InvitationStatus,
    LastOwnerError,
    Member,
    NotAMemberError,
    Organization,
    OrganizationError,
    OrganizationNotFoundError,
    OrgRole,
    SlugAlreadyTakenError,
)
from hexcore.darwin.plugins.organization.service import slugify

if t.TYPE_CHECKING:
    # Sólo para el checker: en runtime los resuelve el `__getattr__` de abajo, porque importarlos
    # arrastra sqlalchemy y nombrar el plugin no puede exigir el extra `[sql]`.
    from hexcore.darwin.plugins.organization.models_mixins import (
        InvitationMixin as InvitationMixin,
        MemberMixin as MemberMixin,
        OrganizationMixin as OrganizationMixin,
    )
    from hexcore.darwin.plugins.organization.service import OrganizationService

__all__ = [
    "OrganizationPlugin",
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
    "OrganizationMixin",
    "MemberMixin",
    "InvitationMixin",
    "slugify",
    "get_organization_service",
]


class OrganizationPlugin(DarwinPlugin):
    """
    El plugin de organizaciones.

    Args:
        invitation_ttl: Cuánto vive una invitación. 7 días por default.
        max_members: Techo de miembros por organización, o `None`. Apagado por default: cada
            producto tiene su límite, normalmente ligado al plan.
        include_router: Si aporta su router.
    """

    name = "organization"

    #: El último de la cadena: las organizaciones se construyen **sobre** una identidad ya
    #: establecida, así que su lugar es después de todo lo que autentica y de `impersonate` (60).
    priority = 80

    def __init__(
        self,
        *,
        invitation_ttl: timedelta | None = None,
        max_members: int | None = None,
        organization_repository: AbstractOrganizationRepository | None = None,
        member_repository: AbstractMemberRepository | None = None,
        invitation_repository: AbstractInvitationRepository | None = None,
        include_router: bool = True,
    ) -> None:
        self._ttl = invitation_ttl
        self._max = max_members
        self._orgs = organization_repository
        self._members = member_repository
        self._invitations = invitation_repository
        self._include_router = include_router
        self._lock = threading.RLock()
        self._service: "OrganizationService | None" = None

    # ── El servicio ───────────────────────────────────────────────────────────
    def service(self) -> "OrganizationService":
        """
        El servicio, construido perezosamente desde el contenedor de identidad.

        Perezoso y cacheado con `RLock`, igual que los proveedores del contenedor: el plugin se
        instancia al declarar el registro —antes de `configure_identity`— así que construirlo en
        `__init__` obligaría a un orden de cableado que nadie tiene por qué recordar.
        """
        with self._lock:
            if self._service is None:
                from hexcore.darwin.application.container import get_identity_container
                from hexcore.darwin.plugins.organization.repository import (
                    SqlAlchemyInvitationRepository,
                    SqlAlchemyMemberRepository,
                    SqlAlchemyOrganizationRepository,
                )
                from hexcore.darwin.plugins.organization.service import (
                    DEFAULT_INVITATION_TTL,
                    OrganizationService,
                )

                contenedor = get_identity_container()
                self._service = OrganizationService(
                    organizations=self._orgs or SqlAlchemyOrganizationRepository(),
                    members=self._members or SqlAlchemyMemberRepository(),
                    invitations=self._invitations
                    or SqlAlchemyInvitationRepository(),
                    users=contenedor.users(),
                    clock=contenedor.clock(),
                    invitation_ttl=self._ttl or DEFAULT_INVITATION_TTL,
                    max_members=self._max,
                )
            return self._service

    def reset(self) -> None:
        """Descarta el servicio cacheado. Para los tests, que reconfiguran el contenedor."""
        with self._lock:
            self._service = None

    # ── Lo que aporta ─────────────────────────────────────────────────────────
    def tables(self) -> t.Mapping[str, type]:
        from hexcore.darwin.plugins.organization.models_mixins import (
            InvitationMixin,
            MemberMixin,
            OrganizationMixin,
        )

        return {
            "OrganizationMixin": OrganizationMixin,
            "MemberMixin": MemberMixin,
            "InvitationMixin": InvitationMixin,
        }

    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        return ORGANIZATION_EXCEPTION_STATUS_MAP

    def routers(self) -> t.Sequence[t.Any]:
        if not self._include_router:
            return ()

        from hexcore.darwin.plugins.organization.router import (
            build_organization_router,
        )

        return [build_organization_router()]


def get_organization_service() -> "OrganizationService":
    """
    El servicio del plugin registrado en este despliegue.

    Se busca en el registro del contenedor y no en un global propio: los plugins son de un
    despliegue, y un segundo global tendría que resetearse aparte en cada test.

    Raises:
        RuntimeError: el plugin no está registrado, con la remediación copiable.
    """
    from hexcore.darwin.application.container import get_identity_container

    plugin = get_identity_container().plugins.get(OrganizationPlugin.name)
    if not isinstance(plugin, OrganizationPlugin):
        raise RuntimeError(
            "El plugin 'organization' no está registrado en este despliegue.\n\n"
            "    from hexcore.darwin import PluginRegistry, configure_identity\n"
            "    from hexcore.darwin.plugins.organization import OrganizationPlugin\n\n"
            "    configure_identity(config, plugins=PluginRegistry([OrganizationPlugin()]))"
        )
    return plugin.service()


def __getattr__(name: str) -> t.Any:
    """
    Los tres mixins, perezosos: importarlos arrastra sqlalchemy.

    Están en `__all__` porque son parte de la API pública —el consumidor los compone en su paquete
    ``models/``— pero nombrar el plugin no puede exigir el extra `[sql]`. Es el mismo patrón que la
    fachada de Darwin y que los plugins de OAuth y passkey.
    """
    if name in ("OrganizationMixin", "MemberMixin", "InvitationMixin"):
        from hexcore.darwin.plugins.organization import models_mixins

        return getattr(models_mixins, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
