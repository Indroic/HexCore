"""
`HookMiddleware`: los hooks de los plugins, sobre la cadena de middlewares que ya existe.

No hay mecanismo nuevo. Un hook es una función pura que recibe el payload y devuelve un
reemplazo (o `None` para no cambiar nada), y este middleware es el que los corre en orden
alrededor del handler. Se registra en el `MiddlewarePipeline` como cualquier otro.

Por qué hooks y no que cada plugin aporte su propio `AbstractMiddleware`: un middleware ve
**todos** los mensajes y tiene que filtrar; un hook declara a qué acciones se engancha y el
middleware hace el filtrado una sola vez, memoizado. Con veinte plugins, la diferencia es
veinte `isinstance` por mensaje contra una búsqueda en un dict.

Los mensajes de HexCore son `frozen`, así que un hook **no puede** mutar el payload aunque
quiera: tiene que devolver una instancia nueva (`model_copy(update=...)`). Eso es deliberado,
y es la diferencia con los hooks de Better Auth, que mutan un `ctx` compartido.
"""
from __future__ import annotations

import logging
import typing as t

from hexcore.darwin.domain.plugins import ShortCircuit, action_of
from hexcore.domain.cqrs.middleware import AbstractMiddleware, NextHandler

if t.TYPE_CHECKING:
    from hexcore.darwin.application.plugins import PluginRegistry

logger = logging.getLogger("hexcore.darwin.hooks")

__all__ = ["HookMiddleware"]


class HookMiddleware(AbstractMiddleware):
    """
    Corre los hooks `before` y `after` de los plugins alrededor del handler.

    Uso::

        pipeline = MiddlewarePipeline([HookMiddleware(registro_de_plugins)])
        bus = InMemoryCommandBus(registry=registry, pipeline=pipeline)
    """

    def __init__(self, plugins: "PluginRegistry") -> None:
        self._plugins = plugins

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        accion = action_of(message)

        try:
            payload = await self._correr(accion, "before", message)
        except ShortCircuit as corto:
            # Un `before` que cortocircuita **no ejecuta el handler**. Es el mecanismo con el
            # que un plugin responde por su cuenta: 2FA que exige el segundo factor, un
            # bloqueo por país, una cuota agotada.
            logger.debug("Hook cortocircuitó '%s' en before.", accion)
            return corto.result

        resultado = await next_handler(payload)

        try:
            return await self._correr(accion, "after", resultado)
        except ShortCircuit as corto:
            # En `after` el handler **ya corrió**: cortocircuitar acá reemplaza el resultado,
            # no cancela el efecto. Un plugin que quiera impedir la operación tiene que
            # hacerlo en `before`.
            logger.debug("Hook reemplazó el resultado de '%s' en after.", accion)
            return corto.result

    async def _correr(self, accion: str, fase: str, valor: t.Any) -> t.Any:
        """
        Corre los hooks de una fase, encadenando el valor.

        Un hook que devuelve `None` **no cambia nada** —es lo que hace la mayoría, que sólo
        observa— y uno que devuelve un valor lo reemplaza para el hook siguiente y para el
        handler. Encadenar y no acumular: así un hook puede refinar lo que hizo el anterior.

        Cualquier excepción que no sea `ShortCircuit` **propaga**: el plugin falla cerrando.
        Tragarla dejaría que un hook de autorización que explota se lea como uno que autorizó.
        """
        for binding in self._plugins.hooks_for(accion, fase):
            try:
                devuelto = await binding.handler(valor)
            except ShortCircuit:
                raise
            except Exception as exc:
                logger.exception(
                    "El hook %s del plugin '%s' falló en %s de '%s'.",
                    getattr(binding.handler, "__qualname__", binding.handler),
                    binding.plugin,
                    fase,
                    accion,
                )
                raise RuntimeError(
                    f"El hook del plugin '{binding.plugin}' falló en la fase '{fase}' de la "
                    f"acción '{accion}'. La operación se aborta: un hook que falla no puede "
                    f"leerse como un hook que aprobó."
                ) from exc

            if devuelto is not None:
                valor = devuelto

        return valor
