"""
Los hooks de los plugins: el runner, y el middleware que lo usa sobre el bus.

No hay mecanismo nuevo. Un hook es una función que recibe el payload y devuelve un reemplazo
(o `None` para no cambiar nada), y `run_hooks` es el que los corre en orden.

Por qué hooks y no que cada plugin aporte su propio `AbstractMiddleware`: un middleware ve
**todos** los mensajes y tiene que filtrar; un hook declara a qué acciones se engancha y el
filtrado se hace una sola vez, memoizado. Con veinte plugins, la diferencia es veinte
`isinstance` por mensaje contra una búsqueda en un dict.

Los mensajes de HexCore son `frozen`, así que un hook **no puede** mutar el payload aunque
quiera: tiene que devolver una instancia nueva (`model_copy(update=...)`). Eso es deliberado,
y es la diferencia con los hooks de Better Auth, que mutan un `ctx` compartido.

**Hay dos caminos, y los dos usan el mismo runner.** `HookMiddleware` cubre lo que pasa por el
bus de comandos. Pero el router de identidad llama a los servicios **directo** —no despacha
comandos— así que un plugin que sólo se enganchara al bus no vería un sign-in por HTTP. Por eso
los servicios llaman a `run_hooks` en sus puntos de extensión declarados. Sin eso, `two_factor`
sería inaplicable al único flujo que importa.
"""
from __future__ import annotations

import logging
import typing as t

from hexcore.darwin.domain.exceptions import IdentityError
from hexcore.darwin.domain.plugins import ShortCircuit, action_of
from hexcore.domain.cqrs.middleware import AbstractMiddleware, NextHandler

if t.TYPE_CHECKING:
    from hexcore.darwin.application.plugins import PluginRegistry
    from hexcore.darwin.domain.plugins import HookPhase

logger = logging.getLogger("hexcore.darwin.hooks")

__all__ = ["HookMiddleware", "run_hooks"]


async def run_hooks(
    plugins: "PluginRegistry | None",
    action: str,
    phase: "HookPhase",
    payload: t.Any,
) -> t.Any:
    """
    Corre los hooks de una acción y fase, encadenando el valor.

    Un hook que devuelve `None` **no cambia nada** —es lo que hace la mayoría, que sólo
    observa— y uno que devuelve un valor lo reemplaza para el hook siguiente y para el
    handler. Encadenar y no acumular: así un hook puede refinar lo que hizo el anterior.

    Qué pasa con las excepciones, que es la parte que importa:

    - `ShortCircuit` **propaga tal cual**. El llamador decide qué significa: en el bus,
      reemplaza el resultado; en un servicio, aborta la operación.
    - Un `IdentityError` **también propaga tal cual**, porque es una señal deliberada del
      dominio: así `two_factor` corta un sign-in con `TwoFactorRequiredError` y el borde HTTP
      lo mapea a su status. Envolverlo lo convertiría en un 500.
    - Cualquier otra excepción se envuelve en un `RuntimeError` que nombra al plugin, la fase
      y la acción, y **propaga**. El plugin falla cerrando: tragarla dejaría que un hook de
      autorización que explota se lea como uno que autorizó.

    `plugins=None` devuelve el payload sin tocarlo, así que un servicio puede llamar a esto
    incondicionalmente sin ramificar.
    """
    if plugins is None:
        return payload

    valor = payload
    for binding in plugins.hooks_for(action, phase):
        try:
            devuelto = await binding.handler(valor)
        except (ShortCircuit, IdentityError):
            raise
        except Exception as exc:
            logger.exception(
                "El hook %s del plugin '%s' falló en %s de '%s'.",
                getattr(binding.handler, "__qualname__", binding.handler),
                binding.plugin,
                phase,
                action,
            )
            raise RuntimeError(
                f"El hook del plugin '{binding.plugin}' falló en la fase '{phase}' de la "
                f"acción '{action}'. La operación se aborta: un hook que falla no puede "
                f"leerse como un hook que aprobó."
            ) from exc

        if devuelto is not None:
            valor = devuelto

    return valor


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
            payload = await run_hooks(self._plugins, accion, "before", message)
        except ShortCircuit as corto:
            # Un `before` que cortocircuita **no ejecuta el handler**. Es el mecanismo con el
            # que un plugin responde por su cuenta: 2FA que exige el segundo factor, un
            # bloqueo por país, una cuota agotada.
            logger.debug("Hook cortocircuitó '%s' en before.", accion)
            return corto.result

        resultado = await next_handler(payload)

        try:
            return await run_hooks(self._plugins, accion, "after", resultado)
        except ShortCircuit as corto:
            # En `after` el handler **ya corrió**: cortocircuitar acá reemplaza el resultado,
            # no cancela el efecto. Un plugin que quiera impedir la operación tiene que
            # hacerlo en `before`.
            logger.debug("Hook reemplazó el resultado de '%s' en after.", accion)
            return corto.result
