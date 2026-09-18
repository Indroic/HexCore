"""
`DrbacAuthorizationProvider`: el `AuthorizationProvider` de `drbac` para `AuthorizationEngine`
(Fase F0).

A diferencia de `RbacAuthorizationProvider` —que sólo concede—, éste puede **denegar
explícitamente**: es la mitad del punto de DRBAC ("sos accountant, pero no podés aprobar tu
propia factura"). Toda la lógica vive en `PolicyDecisionPoint`; este módulo es el adaptador
fino que lo conecta al contrato de `AuthorizationProvider`.
"""
from __future__ import annotations

from hexcore.darwin.domain.authorization import AccessRequest, AuthorizationProvider, Decision
from hexcore.darwin.plugins.drbac.pdp import PolicyDecisionPoint

__all__ = ["DrbacAuthorizationProvider"]


class DrbacAuthorizationProvider(AuthorizationProvider):
    """
    Uso::

        engine = AuthorizationEngine([
            RbacAuthorizationProvider(...),
            DrbacAuthorizationProvider(pdp=pdp),
        ])
    """

    name = "drbac"

    def __init__(self, *, pdp: PolicyDecisionPoint) -> None:
        self._pdp = pdp

    async def decide(self, request: AccessRequest) -> Decision:
        return await self._pdp.decide(request)
