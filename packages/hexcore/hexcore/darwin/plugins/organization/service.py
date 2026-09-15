"""
Los flujos de `organization`: crear, invitar, aceptar, cambiar roles y sacar gente.

Todo el plugin existe para sostener dos invariantes, y las dos son bugs clásicos de todo modelo de
organizaciones:

1. ⚠️ **Una organización nunca queda sin `owner`.** Sacar o degradar al último la vuelve
   inadministrable —nadie puede invitar, nadie puede cambiar roles— y salir de ahí requiere un
   `UPDATE` a mano en producción. El chequeo va **adentro de la sentencia** que degrada o saca,
   como subconsulta correlacionada, y no como un conteo previo: contar y después actualizar es
   check-then-act, y dos degradaciones concurrentes dejan la organización sin ninguno. Lo demostró
   un test.
2. ⚠️ **Nadie asciende a alguien por encima de sí mismo, ni actúa sobre un par o un superior.** Sin
   eso, un `admin` se hace `owner` y el modelo de roles es decorativo.

Y una tercera, sobre el flujo de invitación:

3. ⚠️ **La invitación está atada al mail.** Aceptarla exige que la cuenta tenga ese mail, porque
   reenviar el link es exactamente lo que la gente hace — y sin el chequeo, quien lo reciba entra
   con el rol que se le había dado a otro.
"""
from __future__ import annotations

import re
import typing as t
from datetime import timedelta
from uuid import UUID, uuid4

from hexcore.darwin.domain.value_objects import Email
from hexcore.darwin.plugins.organization.domain import (
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
    OrganizationNotFoundError,
    OrgRole,
    SlugAlreadyTakenError,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.ports import AbstractClock, AbstractUserRepository
    from hexcore.darwin.plugins.organization.domain import (
        AbstractInvitationRepository,
        AbstractMemberRepository,
        AbstractOrganizationRepository,
    )

__all__ = [
    "DEFAULT_INVITATION_TTL",
    "MAX_MEMBERS",
    "InvitationIssued",
    "OrganizationService",
    "slugify",
]

#: Cuánto vive una invitación.
#:
#: 7 días: alcanza para que alguien vuelva de una semana de vacaciones, y no tanto como para que un
#: link olvidado en un buzón siga sirviendo meses después. Una invitación vencida se vuelve a
#: mandar en un click.
DEFAULT_INVITATION_TTL = timedelta(days=7)

#: Techo de miembros por organización, o `None` para no limitar.
#:
#: Existe apagado por default —cada producto tiene su propio límite, normalmente ligado al plan—
#: pero el parámetro está para que quien lo necesite no tenga que envolver el servicio.
MAX_MEMBERS: int | None = None

_SLUG_INVALIDO = re.compile(r"[^a-z0-9]+")


def slugify(nombre: str) -> str:
    """
    Un slug a partir de un nombre.

    Sólo `[a-z0-9-]`, sin guiones al principio ni al final ni repetidos. Deliberadamente pobre: no
    translitera acentos ni maneja alfabetos no latinos, porque un slug generado es una comodidad y
    el consumidor puede pasar el suyo. Lo que **no** hace es dejar pasar caracteres que después
    aparecen escapados en una URL.

    Uso::

        assert slugify("Mi Empresa S.A.") == "mi-empresa-s-a"
    """
    limpio = _SLUG_INVALIDO.sub("-", nombre.strip().lower()).strip("-")
    return limpio or "org"


class InvitationIssued(t.NamedTuple):
    """
    El resultado de invitar.

    Trae el token **en claro**, y es la única vez que existe: la fila guarda su hash. ⚠️ El
    framework no manda mails, así que el llamador tiene que armar el link y mandarlo. No lo
    devuelvas en una ruta pública ni lo loguees.
    """

    invitation: Invitation
    token: str


class OrganizationService:
    """
    Los flujos de organizaciones.

    Uso::

        servicio = get_organization_service()
        org = await servicio.create(name="Mi Empresa", owner_id=usuario.id)
        emitida = await servicio.invite(
            organization_id=org.id, actor_id=usuario.id, email="beto@ejemplo.com"
        )
    """

    def __init__(
        self,
        *,
        organizations: "AbstractOrganizationRepository",
        members: "AbstractMemberRepository",
        invitations: "AbstractInvitationRepository",
        users: "AbstractUserRepository",
        clock: "AbstractClock",
        invitation_ttl: timedelta = DEFAULT_INVITATION_TTL,
        max_members: int | None = MAX_MEMBERS,
    ) -> None:
        self._orgs = organizations
        self._members = members
        self._invitations = invitations
        self._users = users
        self._clock = clock
        self._ttl = invitation_ttl
        self._max = max_members

    # ── Crear y leer ──────────────────────────────────────────────────────────
    async def create(
        self,
        *,
        name: str,
        owner_id: UUID,
        slug: str | None = None,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> Organization:
        """
        Crea la organización y hace `owner` a quien la creó.

        **El `owner` se crea en el mismo flujo y no en un paso aparte.** Una organización sin
        `owner` es inadministrable desde el segundo cero, y dejar los dos pasos separados garantiza
        que alguna vez uno falle en el medio.

        Raises:
            SlugAlreadyTakenError: el slug ya existe.
        """
        candidato = slug or slugify(name)
        if await self._orgs.get_by_slug(candidato) is not None:
            raise SlugAlreadyTakenError(
                f"El identificador {candidato!r} ya está usado. Elegí otro."
            )

        organizacion = await self._orgs.add(
            Organization(
                id=uuid4(),
                name=name,
                slug=candidato,
                metadata=dict(metadata or {}),
            )
        )
        await self._members.add(
            Member(
                id=uuid4(),
                organization_id=organizacion.id,
                user_id=owner_id,
                role=OrgRole.OWNER,
            )
        )
        return organizacion

    async def get(self, organization_id: UUID) -> Organization:
        """
        La organización, o falla.

        Raises:
            OrganizationNotFoundError
        """
        organizacion = await self._orgs.get(organization_id)
        if organizacion is None:
            raise OrganizationNotFoundError("No existe esa organización.")
        return organizacion

    async def get_by_slug(self, slug: str) -> Organization:
        """La organización por slug, o falla."""
        organizacion = await self._orgs.get_by_slug(slug)
        if organizacion is None:
            raise OrganizationNotFoundError("No existe esa organización.")
        return organizacion

    async def update(
        self,
        *,
        organization_id: UUID,
        actor_id: UUID,
        name: str,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> Organization:
        """
        Actualiza nombre y metadata. Requiere ser `admin` o más.

        **El slug no se puede cambiar**, y no es un olvido: un slug que cambia rompe cada link
        guardado, cada bookmark y cada integración que lo tenga fijo. Quien de verdad lo necesite
        crea otra organización y migra.
        """
        await self.require_role(
            organization_id=organization_id, user_id=actor_id, minimum=OrgRole.ADMIN
        )
        actual = await self.get(organization_id)
        return await self._orgs.update(
            actual.model_copy(
                update={"name": name, "metadata": dict(metadata or {})}
            )
        )

    async def list_for_user(self, user_id: UUID) -> list[Member]:
        """Las membresías de alguien, para el selector de la interfaz."""
        return await self._members.list_for_user(user_id)

    async def list_members(
        self, *, organization_id: UUID, actor_id: UUID
    ) -> list[Member]:
        """
        Los miembros. **Exige ser miembro**: la lista de quiénes trabajan en una empresa no es
        pública, y un endpoint que la devuelve sin chequear es una fuente de datos para prospección
        y para ingeniería social.
        """
        await self.require_role(
            organization_id=organization_id, user_id=actor_id, minimum=OrgRole.MEMBER
        )
        return await self._members.list_for_organization(organization_id)

    # ── Autorización ──────────────────────────────────────────────────────────
    async def require_role(
        self, *, organization_id: UUID, user_id: UUID, minimum: OrgRole
    ) -> Member:
        """
        Exige que el usuario sea miembro con al menos ese rol, y devuelve su membresía.

        Es la consulta de autorización del plugin, y es **una lectura por operación con alcance de
        organización**. No va en el token a propósito: un `org_role` en el access token queda
        obsoleto cuando alguien degrada a un `admin`, y seguiría valiendo hasta que el token venza
        — que es exactamente lo que no se quiere de un cambio de permisos.

        Raises:
            NotAMemberError: no es miembro.
            InsufficientOrgRoleError: es miembro pero su rol no alcanza.
        """
        membresia = await self._members.get(organization_id, user_id)
        if membresia is None:
            raise NotAMemberError("No sos miembro de esa organización.")
        if not membresia.role.at_least(minimum):
            raise InsufficientOrgRoleError(
                f"Hace falta ser {minimum} o más para esta operación; tu rol es "
                f"{membresia.role}."
            )
        return membresia

    # ── Invitar ───────────────────────────────────────────────────────────────
    async def invite(
        self,
        *,
        organization_id: UUID,
        actor_id: UUID,
        email: str,
        role: OrgRole = OrgRole.MEMBER,
    ) -> InvitationIssued:
        """
        Invita a alguien. Requiere ser `admin` o más.

        **No se puede invitar con un rol mayor al propio.** Sin ese chequeo, un `admin` invita a un
        cómplice como `owner` y desde ahí lo tiene todo — es la escalada más barata del modelo, y
        pasa por un endpoint que suena inofensivo.

        Raises:
            NotAMemberError, InsufficientOrgRoleError
            AlreadyAMemberError: ya está en la organización.
            ValueError: se alcanzó el techo de miembros.
        """
        from hexcore.darwin.infrastructure.hashing import generate_token, hash_token

        actor = await self.require_role(
            organization_id=organization_id, user_id=actor_id, minimum=OrgRole.ADMIN
        )
        if role.outranks(actor.role):
            raise InsufficientOrgRoleError(
                f"No podés invitar con el rol {role} porque el tuyo es {actor.role}: nadie "
                f"asciende a alguien por encima de sí mismo."
            )

        normalizado = Email(value=email).value

        usuario = await self._users.get_by_email(normalizado)
        if usuario is not None and await self._members.get(
            organization_id, usuario.id
        ):
            raise AlreadyAMemberError("Esa persona ya es miembro de la organización.")

        await self._verificar_techo(organization_id)

        ahora = self._clock.now()
        token = generate_token()
        invitacion = await self._invitations.add(
            Invitation(
                id=uuid4(),
                organization_id=organization_id,
                email=normalizado,
                role=role,
                invited_by=actor_id,
                token_hash=hash_token(token),
                expires_at=ahora + self._ttl,
            )
        )
        return InvitationIssued(invitation=invitacion, token=token)

    async def accept_invitation(self, *, token: str, user_id: UUID) -> Member:
        """
        Acepta la invitación y crea la membresía.

        ⚠️ **Exige que la cuenta tenga el mail de la invitación.** Reenviar el link es exactamente
        lo que la gente hace, y sin este chequeo quien lo reciba entra con el rol que se le había
        dado a otro. El mail tiene que estar **verificado**: si no, alguien registra una cuenta con
        el mail de un invitado y le roba la invitación sin acceso a la casilla.

        Raises:
            InvitationError: no existe, venció, ya se usó o fue revocada.
            InvitationEmailMismatchError: la cuenta no tiene ese mail, o no lo verificó.
            AlreadyAMemberError: ya era miembro.
        """
        from hexcore.darwin.infrastructure.hashing import hash_token

        usuario = await self._users.get_by_id(user_id)
        if usuario is None:
            raise InvitationError("La cuenta que acepta no existe.")

        # ⚠️ El mail se chequea **antes** de consumir la invitación: consumirla primero y rechazar
        # después la gastaría, y el invitado legítimo tendría que pedir otra por un intento que no
        # era suyo.
        invitacion = await self._buscar_pendiente(hash_token(token))
        if usuario.email != invitacion.email:
            raise InvitationEmailMismatchError(
                "Esa invitación es para otra dirección de correo. Iniciá sesión con la cuenta "
                "invitada."
            )
        if not usuario.email_verified:
            raise InvitationEmailMismatchError(
                "Verificá tu dirección de correo antes de aceptar la invitación: si no, "
                "cualquiera que registre esa dirección podría tomar la invitación."
            )

        if await self._members.get(invitacion.organization_id, user_id) is not None:
            raise AlreadyAMemberError("Ya sos miembro de esa organización.")

        await self._verificar_techo(invitacion.organization_id)

        ahora = self._clock.now()
        consumida = await self._invitations.consume(hash_token(token), at=ahora)
        if consumida is None:
            # Otra petición ganó la carrera. Es el caso que hace atómico el `consume`: sin él, las
            # dos crearían una membresía y el `UNIQUE` rechazaría la segunda con un error de base.
            raise InvitationError(
                "La invitación no es válida, venció o ya se usó."
            )

        return await self._members.add(
            Member(
                id=uuid4(),
                organization_id=consumida.organization_id,
                user_id=user_id,
                role=consumida.role,
            )
        )

    async def revoke_invitation(
        self, *, organization_id: UUID, actor_id: UUID, invitation_id: UUID
    ) -> None:
        """
        Revoca una invitación pendiente. Requiere ser `admin` o más.

        Se marca revocada y no se borra: una invitación revocada es información de auditoría — dice
        que alguien invitó y después se arrepintió.

        Raises:
            InvitationError: no existe, no es de esta organización, o ya no está pendiente.
        """
        await self.require_role(
            organization_id=organization_id, user_id=actor_id, minimum=OrgRole.ADMIN
        )

        invitacion = await self._invitations.get(invitation_id)
        if invitacion is None or invitacion.organization_id != organization_id:
            # Mismo error para las dos: decir "existe pero no es tuya" confirmaría la existencia de
            # invitaciones de otras organizaciones.
            raise InvitationError("No se encontró esa invitación.")

        if not await self._invitations.revoke(invitation_id, at=self._clock.now()):
            raise InvitationError("Esa invitación ya no está pendiente.")

    async def list_pending_invitations(
        self, *, organization_id: UUID, actor_id: UUID
    ) -> list[Invitation]:
        """Las invitaciones pendientes. Requiere ser `admin` o más."""
        await self.require_role(
            organization_id=organization_id, user_id=actor_id, minimum=OrgRole.ADMIN
        )
        return await self._invitations.list_pending(organization_id)

    # ── Roles y bajas ─────────────────────────────────────────────────────────
    async def set_role(
        self,
        *,
        organization_id: UUID,
        actor_id: UUID,
        target_user_id: UUID,
        role: OrgRole,
    ) -> Member:
        """
        Cambia el rol de alguien. Requiere ser `admin` o más.

        Tres chequeos, y cada uno cierra una escalada distinta:

        1. **No se asciende por encima de uno mismo.** Si no, un `admin` se pone `owner`.
        2. **No se actúa sobre un par ni sobre un superior.** Si no, un `admin` degrada a otro
           `admin` —o al `owner`— y se queda solo arriba.
        3. **No se degrada al último `owner`.** Ver la invariante 1 del módulo.

        Raises:
            NotAMemberError, InsufficientOrgRoleError, LastOwnerError
        """
        actor = await self.require_role(
            organization_id=organization_id, user_id=actor_id, minimum=OrgRole.ADMIN
        )

        objetivo = await self._members.get(organization_id, target_user_id)
        if objetivo is None:
            raise NotAMemberError("Esa persona no es miembro de la organización.")

        if role.outranks(actor.role):
            raise InsufficientOrgRoleError(
                f"No podés asignar el rol {role} porque el tuyo es {actor.role}."
            )

        # El actor sobre sí mismo se permite sólo para bajar de nivel, y eso lo cubre el chequeo
        # del último owner. Sobre otro, tiene que estar estrictamente por encima.
        if target_user_id != actor_id and not actor.role.outranks(objetivo.role):
            raise InsufficientOrgRoleError(
                f"No podés cambiarle el rol a alguien que es {objetivo.role} siendo "
                f"{actor.role}: sólo se administra hacia abajo."
            )

        # El guardián va **adentro** de la sentencia: ver la invariante 1 del módulo. Se pide sólo
        # cuando el cambio saca un owner — ascender a owner nunca deja la organización sin ninguno.
        saca_un_owner = objetivo.role is OrgRole.OWNER and role is not OrgRole.OWNER

        actualizado = await self._members.set_role(
            organization_id,
            target_user_id,
            role=role,
            keep_last_owner=saca_un_owner,
        )
        if actualizado is None:
            if saca_un_owner:
                # La sentencia no aplicó porque no queda otro owner. Se distingue del "no es
                # miembro" mirando de nuevo: la fila se acaba de leer, así que si sigue estando, lo
                # que bloqueó fue el guardián.
                raise LastOwnerError(
                    "No se puede dejar la organización sin ningún owner: quedaría sin nadie que "
                    "pueda administrarla. Nombrá otro owner primero."
                )
            raise NotAMemberError("Esa persona no es miembro de la organización.")
        return actualizado

    async def remove_member(
        self, *, organization_id: UUID, actor_id: UUID, target_user_id: UUID
    ) -> None:
        """
        Saca a alguien de la organización.

        **Irse uno mismo siempre se permite** —salvo que seas el último `owner`— y no requiere
        ningún rol: nadie tiene que pedir permiso para dejar de trabajar en un lugar. Sacar a otro
        requiere ser `admin` o más y estar estrictamente por encima.

        Raises:
            NotAMemberError, InsufficientOrgRoleError, LastOwnerError
        """
        propio = target_user_id == actor_id

        if propio:
            objetivo = await self._members.get(organization_id, actor_id)
            if objetivo is None:
                raise NotAMemberError("No sos miembro de esa organización.")
        else:
            actor = await self.require_role(
                organization_id=organization_id,
                user_id=actor_id,
                minimum=OrgRole.ADMIN,
            )
            objetivo = await self._members.get(organization_id, target_user_id)
            if objetivo is None:
                raise NotAMemberError("Esa persona no es miembro de la organización.")
            if not actor.role.outranks(objetivo.role):
                raise InsufficientOrgRoleError(
                    f"No podés sacar a alguien que es {objetivo.role} siendo {actor.role}."
                )

        # Igual que en `set_role`: el guardián va adentro de la sentencia.
        es_owner = objetivo.role is OrgRole.OWNER
        if not await self._members.remove(
            organization_id, target_user_id, keep_last_owner=es_owner
        ) and es_owner:
            raise LastOwnerError(
                "No se puede dejar la organización sin ningún owner: quedaría sin nadie que pueda "
                "administrarla. Nombrá otro owner primero."
            )

    async def delete(self, *, organization_id: UUID, actor_id: UUID) -> None:
        """
        Borra la organización. **Sólo un `owner`.**

        Las membresías y las invitaciones se van por `CASCADE`. No hay confirmación acá: eso es del
        borde, y meterla en el servicio la haría imposible de saltear desde un script legítimo.
        """
        await self.require_role(
            organization_id=organization_id, user_id=actor_id, minimum=OrgRole.OWNER
        )
        if not await self._orgs.delete(organization_id):
            raise OrganizationNotFoundError("No existe esa organización.")

    # ── Interno ───────────────────────────────────────────────────────────────
    async def count_owners(self, organization_id: UUID) -> int:
        """
        Cuántos `owner` hay. **Informativo**: para la interfaz, no para decidir.

        La decisión de si se puede degradar o sacar al último la toma la base, adentro de la
        sentencia — ver la invariante 1 del módulo. Este conteo existe para poder mostrarle al
        usuario "sos el único owner" antes de que apriete el botón.
        """
        return await self._members.count_by_role(organization_id, OrgRole.OWNER)

    async def _verificar_techo(self, organization_id: UUID) -> None:
        if self._max is None:
            return
        actuales = len(await self._members.list_for_organization(organization_id))
        if actuales >= self._max:
            raise ValueError(
                f"La organización alcanzó el máximo de {self._max} miembros."
            )

    async def _buscar_pendiente(self, token_hash: str) -> Invitation:
        """
        La invitación pendiente y vigente, **sin consumirla**.

        Se separa del consumo para poder chequear el mail antes: consumirla primero y rechazar
        después la gastaría, y el invitado legítimo tendría que pedir otra por un intento que no
        era suyo. El consumo posterior es igual atómico, así que la carrera entre dos aceptaciones
        la resuelve la base y no esta lectura.

        **Un solo error para los cuatro casos** —no existe, venció, ya se usó, fue revocada—
        porque distinguirlos le diría a quien prueba tokens si acertó uno que existe.
        """
        invitacion = await self._invitations.get_by_token_hash(token_hash)
        if (
            invitacion is None
            or invitacion.status is not InvitationStatus.PENDING
            or invitacion.expires_at <= self._clock.now()
        ):
            raise InvitationError("La invitación no es válida, venció o ya se usó.")
        return invitacion
