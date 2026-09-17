"""
El contrato de autorización: "¿puede este actor hacer esto sobre este recurso?", agnóstico de
**quién** contesta.

Hasta acá Darwin sólo sabía preguntar por un scope exacto (`AuthContext.has_scope`) o por un
rol (`RoleRegistry.has_permission`). Ninguno de los dos alcanza para "aprobá esta factura,
salvo que sea la tuya" — una decisión que depende del **recurso**, no sólo del actor. Este
módulo separa la pregunta (`AccessRequest`) de la respuesta (`Decision`) de quien la contesta
(`AuthorizationProvider`): así RBAC, DRBAC y un scope suelto pueden convivir detrás de la misma
forma, y `AuthorizationEngine` (capa de aplicación) los combina sin que ninguno sepa de los
otros.

Los tres efectos posibles, y por qué no son dos:

- ``allow`` / ``deny``: la respuesta de un provider al que la pregunta **le compete**.
- ``not_applicable``: "esto no es asunto mío". Sin este tercer valor, un provider de DRBAC que
  sólo conoce `invoice.*` tendría que devolver `deny` para `user.delete` — y un `deny` de un
  provider que ni siquiera entendió la pregunta taparía el `allow` de RBAC bajo deny-overrides.
  `not_applicable` es lo que hace posible que varios providers convivan sin pisarse.

Módulo de dominio puro: stdlib + pydantic, como `context.py`. Sin sqlalchemy, sin Starlette,
sin crypto — lo importan el motor de aplicación y cualquier plugin que aporte un provider.
"""
from __future__ import annotations

import abc
import typing as t
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hexcore.darwin.domain.context import AuthContext

__all__ = [
    "Effect",
    "ResourceRef",
    "AccessRequest",
    "Decision",
    "PermissionSnapshot",
    "AuthorizationProvider",
]

#: ``not_applicable`` existe para que varios providers convivan sin pisarse. Ver el docstring
#: del módulo.
Effect = t.Literal["allow", "deny", "not_applicable"]


class ResourceRef(BaseModel):
    """
    El recurso sobre el que se pide la decisión.

    `None` en `AccessRequest.resource` es una acción sin recurso —sobre la colección, como
    `invoice.create`—, y ahí ni siquiera hace falta construir uno.

    `attributes` son los que el llamador **ya tiene a mano** —los que vinieron en el path o en
    el propio mensaje— y no un lugar para volcar el recurso entero: lo que falte para evaluar
    una condición lo completa el PIP de DRBAC bajo demanda, no acá.
    """

    model_config = ConfigDict(frozen=True)

    #: `"invoice"`, `"user"`. Es la clave con la que un provider indexa sus reglas.
    type: str = Field(min_length=1)
    #: `None` cuando la acción es sobre la colección (`invoice.create`) y no sobre una fila.
    id: str | None = None
    owner_id: UUID | None = None
    #: Ruta materializada de tenancy/jerarquía, p.ej. `"org:42/project:7"`. Vacía = sin scope,
    #: y por lo tanto sin herencia que aplicar.
    scope_path: str = ""
    attributes: t.Mapping[str, t.Any] = Field(default_factory=dict)


class AccessRequest(BaseModel):
    """
    Lo que se le pregunta al motor: quién, qué acción, sobre qué recurso, en qué entorno.

    Siempre lleva `context.actor` a la decisión, nunca `context.subject` — la misma regla que
    `AuthContext.has_scope` ya aplica: en una impersonación, lo que se puede hacer lo determina
    quien ejecuta. Al revés, impersonar a un admin sería una escalación de privilegios en un
    solo paso.
    """

    model_config = ConfigDict(frozen=True)

    context: AuthContext[t.Any]
    #: `"invoice.approve"`. La misma forma `recurso.verbo` que ya usan los permisos de
    #: `domain/permissions.py`, para que un scope de rol y una acción de autorización se lean
    #: igual.
    action: str = Field(min_length=1)
    resource: ResourceRef | None = None
    #: Lo que un provider puede necesitar y el dominio no conoce por sí solo: ip, hora, si hubo
    #: MFA, el transporte. Vacío por defecto porque `ScopeAuthorizationProvider` no lo usa.
    environment: t.Mapping[str, t.Any] = Field(default_factory=dict)


class Decision(BaseModel):
    """
    El veredicto de un provider, o del motor una vez combinados todos.

    `reason` es **interno**: para el log y la auditoría, nunca para el cliente. Un 403 que
    explicara la política ("te falta el rol admin", "esta factura no es tuya") le regala a
    quien está sondeando el sistema un mapa de lo que existe adentro; por eso
    `AccessDeniedError` sólo lleva `required` en el body, nunca `reason`.
    """

    model_config = ConfigDict(frozen=True)

    effect: Effect
    #: Quién decidió. Va en la auditoría y en los logs de un fallo del motor.
    provider: str
    policy_id: str | None = None
    reason: str = ""
    #: Efectos secundarios que la decisión pide, no que ejecuta: `"audit"`, `"require_mfa"`.
    #: Quien orquesta (`AuthorizationEngine`) es quien los cumple.
    obligations: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


class PermissionSnapshot(BaseModel):
    """
    Lo que un provider le puede resumir a un cliente para que evalúe **de forma optimista**,
    sin pegarle al servidor en cada render.

    Es sólo eso: optimismo de UI. La autoridad sigue siendo `AuthorizationEngine.decide()`
    corriendo en el servidor ante cada acción real; este resumen nunca se usa para decidir
    nada del lado del servidor, y por eso puede quedarse corto sin que sea un problema de
    seguridad — a lo sumo esconde o muestra de más un botón hasta la próxima revalidación.
    """

    model_config = ConfigDict(frozen=True)

    #: Identifica la versión de este resumen; un cliente lo usa para invalidar su cache local.
    version: str
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    scope_path: str = ""
    #: Reglas evaluables en el cliente (DRBAC), ya filtradas de las que no lo son. Sin datos
    #: sensibles: esto viaja al navegador.
    client_policies: tuple[t.Mapping[str, t.Any], ...] = ()
    expires_at: datetime


class AuthorizationProvider(abc.ABC):
    """
    Quién decide. `ScopeAuthorizationProvider` (capa de aplicación) es el único que shippea
    este módulo; RBAC y DRBAC van a aportar el suyo desde sus propios plugins, y un consumidor
    puede aportar el suyo vía `DarwinPlugin.authorization_providers()`.

    Sólo `decide` es abstracto. `permissions_snapshot` es **concreto y vacío por default**,
    mismo criterio de diseño que `DarwinPlugin`: un provider puramente contextual —uno de
    DRBAC que sólo sabe "permitido si sos el dueño"— no tiene nada que resumirle a un cliente
    de antemano, y obligarlo a implementarlo sería pedirle que invente una respuesta.
    """

    #: Identificador del provider. Aparece en `Decision.provider` y en los logs del motor
    #: cuando este provider falla decidiendo.
    name: t.ClassVar[str] = "unnamed"

    @abc.abstractmethod
    async def decide(self, request: AccessRequest) -> Decision:
        """
        La decisión de este provider para `request`.

        Devolvé `not_applicable` cuando la pregunta no te compete —una acción que no
        reconocés, un recurso de un tipo que no manejás— y no un `deny`: un `deny` de un
        provider que ni entendió la pregunta taparía bajo deny-overrides el `allow` de uno
        que sí sabía.

        No está pensado para lanzar como forma de decir "no aplica" o "deniego": una
        excepción acá la interpreta `AuthorizationEngine` como `deny` fail-closed y la loguea
        como una falla del provider, no como una decisión — mismo criterio que `run_hooks`
        con un hook que explota.
        """
        raise NotImplementedError

    async def permissions_snapshot(
        self, context: AuthContext[t.Any], *, scope_path: str = ""
    ) -> PermissionSnapshot:
        """Vacío por default: versión `"0"`, sin roles ni permisos, ya vencido al crearlo."""
        del context
        return PermissionSnapshot(
            version="0", scope_path=scope_path, expires_at=datetime.now(UTC)
        )
