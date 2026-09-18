"""
El Policy Information Point: completa `resource.*` para una condición que necesita atributos
que `ResourceRef` no traía.

`ResourceRef.attributes` (Fase F0) es lo que el llamador **ya tenía a mano** —lo que vino en el
path o en el propio comando—, a propósito acotado: cargar el recurso entero "por las dudas" en
cada endpoint protegido sería trabajo desperdiciado la mayoría de las veces, porque la mayoría
de las condiciones no lo necesitan. Cuando sí hace falta —``resource.status`` para "no aprobar
tu propia factura salvo que siga en borrador"— un `ResourceAttributeResolver` registrado por
`resource.type` completa lo que falte, bajo demanda.

**Memoizado, con límite y vencimiento.** El `DrbacPlugin` cachea un único `PolicyDecisionPoint`
—y con él, un único `PolicyInformationPoint`— para toda la vida del proceso, no por lote de
decisiones: un `POST /auth/drbac/check` no es el único llamador, y nada resetea el PIP entre
peticiones. Por eso la memoización tiene que ser segura para vivir todo el proceso: acotada en
tamaño (LRU) y con vencimiento (TTL), no un `dict` que sólo crece.
"""
from __future__ import annotations

import abc
import logging
import time
import typing as t
from collections import OrderedDict

from hexcore.darwin.domain.authorization import ResourceRef

__all__ = ["ResourceAttributeResolver", "PolicyInformationPoint"]

logger = logging.getLogger("hexcore.darwin.drbac")

#: Sentinela para el `id` de un recurso sin id — distinto de `""`, que sí podría ser un id real
#: en un backend que no valida el formato. Colapsar `None` a `""` mezclaba en la misma entrada
#: de caché todo recurso sin id de un mismo `type`, así que un resolver typeless-pero-variable
#: (poco común, pero legal) devolvía siempre el primer resultado que calculó.
_SIN_ID = object()

#: Tamaño y vencimiento por defecto del caché — generosos para un lote de `/check` (50 ítems,
#: Fase F5) y cortos para no acumular memoria entre lotes ni servir un atributo resuelto hace
#: rato como si fuera fresco.
_MAXSIZE_POR_DEFECTO = 512
_TTL_POR_DEFECTO_S = 30.0


class ResourceAttributeResolver(abc.ABC):
    """
    Completa los atributos de un tipo de recurso.

    Uso::

        class InvoiceAttributes(ResourceAttributeResolver):
            resource_type = "invoice"

            def __init__(self, uow_scope): self._uow_scope = uow_scope

            async def resolve(self, resource: ResourceRef) -> Mapping[str, Any]:
                async with self._uow_scope() as uow:
                    factura = await uow.invoices.get(UUID(resource.id))
                return {"status": factura.status, "amount": factura.amount}
    """

    #: Qué `ResourceRef.type` resuelve este adaptador.
    resource_type: t.ClassVar[str]

    @abc.abstractmethod
    async def resolve(self, resource: ResourceRef) -> t.Mapping[str, t.Any]:
        """
        Los atributos adicionales de `resource`. Nunca los que ya trae `resource.attributes`
        —eso lo mergea el PIP, no este método—, sólo lo que hace falta ir a buscar.
        """
        raise NotImplementedError


class PolicyInformationPoint:
    """
    Uso::

        pip = PolicyInformationPoint(resolvers={"invoice": InvoiceAttributes(uow_scope)})
        atributos = await pip.attributes_for(resource_ref)
    """

    def __init__(
        self,
        *,
        resolvers: t.Mapping[str, ResourceAttributeResolver] | None = None,
        maxsize: int = _MAXSIZE_POR_DEFECTO,
        ttl_seconds: float = _TTL_POR_DEFECTO_S,
    ) -> None:
        self._resolvers = dict(resolvers or {})
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        #: `OrderedDict` para poder desalojar por LRU (`move_to_end` en cada hit,
        #: `popitem(last=False)` cuando se pasa de `maxsize`). El valor lleva el timestamp de
        #: cuándo se calculó, para el vencimiento por TTL.
        self._cache: OrderedDict[tuple[str, t.Any], tuple[float, dict[str, t.Any]]] = (
            OrderedDict()
        )

    async def attributes_for(self, resource: ResourceRef | None) -> dict[str, t.Any]:
        """
        `resource.attributes` completado con lo que el resolver de `resource.type` sepa.

        Un fallo del resolver **no propaga**: se loguea y se sigue sólo con lo que ya venía en
        `resource.attributes`. Mismo criterio que `RbacAuthorizationProvider
        ._con_rol_de_organizacion` — una integración que falla no puede tumbar la decisión
        entera; en el peor caso, la condición que necesitaba el atributo faltante da
        indeterminado, que el PDP trata fail-closed.
        """
        if resource is None:
            return {}

        clave = (resource.type, resource.id if resource.id is not None else _SIN_ID)
        en_cache = self._cache.get(clave)
        if en_cache is not None:
            calculado_en, valor = en_cache
            if time.monotonic() - calculado_en <= self._ttl_seconds:
                self._cache.move_to_end(clave)
                return valor
            del self._cache[clave]

        completados: dict[str, t.Any] = dict(resource.attributes)
        resolver = self._resolvers.get(resource.type)
        if resolver is not None:
            try:
                extra = await resolver.resolve(resource)
                # El resolver gana: es lo que el servidor recalculó a propósito para esta
                # decisión. `resource.attributes` es lo que trajo el llamador —potencialmente
                # el cliente del `/check`— y dejar que pise el atributo resuelto abría la
                # puerta a que cualquiera spoofeara `resource.status`, `resource.owner_id`, lo
                # que sea, con tal de mandarlo en el body. Lo que el llamador aporta y el
                # resolver no conoce sigue pasando: sólo se pisan las claves que el resolver sí
                # calculó.
                completados = {**completados, **extra}
            except Exception:
                logger.warning(
                    "drbac: el PIP de '%s' falló resolviendo atributos para %r; se sigue "
                    "sólo con los atributos que ya traía el ResourceRef.",
                    resource.type,
                    resource.id,
                    exc_info=True,
                )

        self._cache[clave] = (time.monotonic(), completados)
        self._cache.move_to_end(clave)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)
        return completados
