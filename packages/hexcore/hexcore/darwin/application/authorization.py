"""
El motor de autorización: combina las decisiones de los `AuthorizationProvider` cableados.

`AuthorizationEngine` no sabe nada de RBAC ni de DRBAC — sólo sabe combinar `Decision`s con
deny-overrides y default-deny. Eso es lo que permite que RBAC dé el piso de `allow`s y DRBAC lo
restrinja condicionalmente sin que ninguno de los dos conozca al otro: los dos le hablan al
mismo motor, y el motor es el único que entiende el orden.
"""
from __future__ import annotations

import logging
import typing as t

from hexcore.darwin.domain.authorization import (
    AccessRequest,
    AuthorizationProvider,
    Decision,
    ResourceRef,
)
from hexcore.darwin.domain.context import require_auth
from hexcore.darwin.domain.exceptions import AccessDeniedError
from hexcore.darwin.domain.permissions import Permission

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.ports import AbstractAuditSink

logger = logging.getLogger("hexcore.darwin.authorization")

__all__ = ["AuthorizationEngine", "ScopeAuthorizationProvider", "authorize"]


class ScopeAuthorizationProvider(AuthorizationProvider):
    """
    El provider por defecto: retrocompatible con `Principal.scopes` de antes de este módulo.

    Arregla una asimetría que existía desde antes: `AuthContext.has_scope` (y por lo tanto
    `require_scopes`) hace un **match exacto** — `scope in self.scopes` —, así que un actor
    con el scope `"users.*"` no pasaba `require_scopes("users.invite")` aunque
    `Permission.grants` sabe interpretar ese comodín desde 9.x. Acá sí se usa `Permission.grants`,
    así que un scope con comodín concede por este camino lo que promete.

    Deliberadamente **no** se cambia `Principal.has_scope`: es el chequeo barato y exacto que
    usan `require_scopes`/`require_roles`, y encima corre en un camino caliente que no debería
    empezar a recorrer un `frozenset` con `fnmatch` por cada llamada. Este provider es el lugar
    nuevo, no un parche del viejo.
    """

    name = "scope"

    async def decide(self, request: AccessRequest) -> Decision:
        concedido = any(
            Permission(value=scope).grants(request.action)
            for scope in request.context.actor.scopes
        )
        if concedido:
            return Decision(effect="allow", provider=self.name)
        return Decision(effect="not_applicable", provider=self.name)


class AuthorizationEngine:
    """
    Combina las decisiones de varios `AuthorizationProvider` en una sola.

    Algoritmo, **deny-overrides + default-deny**:

    1. Cualquier `deny` gana, sin importar cuántos `allow` haya. Es el único orden que permite
       que un plugin **restrinja** lo que otro concedió —"sos accountant, pero no podés
       aprobar tu propia factura"— sin que el que restringe tenga que correr *antes* y cortar
       la cadena por su cuenta.
    2. Sin ningún `deny`, el primer `allow` gana.
    3. Si todos los providers dijeron `not_applicable` —el caso de cero providers incluido—
       se deniega. Ningún camino de este motor devuelve "permitido" porque nadie supo
       decidir: es la misma regla de `system_context` de no interpretar la ausencia de
       respuesta como concesión.

    Un provider que **lanza** se trata como si hubiera respondido `deny` y se loguea; el resto
    de los providers igual corre. Mismo criterio de fondo que `run_hooks` con un hook que
    explota —un plugin roto no se lee como uno que aprobó—, pero acá "fallar cerrando" es
    literal: no se envuelve en una excepción que tumbe la request entera, porque un provider de
    autorización roto no puede convertir cada acción protegida en un 500.
    """

    def __init__(
        self,
        providers: t.Sequence[AuthorizationProvider],
        *,
        audit: "AbstractAuditSink | None" = None,
    ) -> None:
        self._providers = tuple(providers)
        self._audit = audit

    async def decide(self, request: AccessRequest) -> Decision:
        decisiones = [
            await self._decidir_con(provider, request) for provider in self._providers
        ]
        resultado = self._combinar(decisiones)

        if self._audit is not None and "audit" in resultado.obligations:
            await self._audit.record(
                action=request.action,
                actor_id=request.context.actor_id,
                subject_id=request.context.subject_id,
                impersonated=request.context.is_impersonating,
                metadata={"effect": resultado.effect, "provider": resultado.provider},
            )

        return resultado

    async def _decidir_con(
        self, provider: AuthorizationProvider, request: AccessRequest
    ) -> Decision:
        nombre = getattr(provider, "name", type(provider).__qualname__)
        try:
            return await provider.decide(request)
        except Exception:
            logger.exception(
                "El provider de autorización '%s' falló decidiendo '%s'. Se deniega: un "
                "provider que explota no puede leerse como uno que autorizó.",
                nombre,
                request.action,
            )
            return Decision(
                effect="deny",
                provider=nombre,
                reason="El provider lanzó una excepción al decidir.",
            )

    @staticmethod
    def _combinar(decisiones: t.Sequence[Decision]) -> Decision:
        for decision in decisiones:
            if decision.effect == "deny":
                return decision
        for decision in decisiones:
            if decision.effect == "allow":
                return decision
        return Decision(
            effect="deny",
            provider="engine",
            reason="Ningún provider autorizó la acción (default-deny).",
        )


async def authorize(
    action: str, resource: ResourceRef | None = None, **environment: t.Any
) -> Decision:
    """
    La forma imperativa: autorizá `action` para el actor en curso, o lanzá.

    Usa `require_auth()` —el `AuthContext` del `ContextVar`—, así que sirve adentro de
    cualquier `auth_scope`/`system_context` en curso: un handler de comando, un endpoint que
    ya pasó por `AuthContextMiddleware`. Para el borde HTTP declarativo está
    `require_permission` (`infrastructure/api/authorization.py`); para CQRS declarativo,
    `authorize_command` (`application/authz_middleware.py`).

    Raises:
        UnauthenticatedError: si no hay `AuthContext` en curso.
        AccessDeniedError: 403, con `required` = `action`.

    Uso::

        async def handle(self, cmd: AprobarFactura) -> None:
            await authorize(
                "invoice.approve",
                ResourceRef(type="invoice", id=str(cmd.invoice_id), owner_id=factura.owner_id),
            )
    """
    from hexcore.darwin.application.container import get_identity_container

    context = require_auth()
    engine = get_identity_container().authorizer()
    decision = await engine.decide(
        AccessRequest(
            context=context, action=action, resource=resource, environment=environment
        )
    )
    if not decision.allowed:
        raise AccessDeniedError(action)
    return decision
