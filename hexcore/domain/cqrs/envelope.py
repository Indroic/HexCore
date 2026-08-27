"""
El sobre de metadata que acompaña a un mensaje CQRS cuando cruza un proceso.

El problema que resuelve: un `@background_command` se serializa en el proceso web y se
ejecuta en un worker. Todo el contexto ambiental —quién estaba autenticado, con qué
permisos— vive en `ContextVar`s, y un `ContextVar` no cruza una cola. El handler que corre
en el worker no tiene forma de saber a nombre de quién actúa.

La solución es un **sobre**: una clave extra en el payload serializado, poblada en el
momento del encolado por *proveedores* registrados, y consumida en el worker por
*restauradores* registrados bajo la misma clave.

**Este módulo no sabe nada de identidad.** Es el punto de extensión; Darwin registra su
proveedor y su restaurador al cablearse (`configure_identity`), y los deregistra al
resetearse. Sin nadie registrado, `collect_envelope_metadata()` devuelve un dict vacío y el
payload queda **byte a byte idéntico** al que el framework generaba antes: es lo que hace
que este mecanismo sea aditivo y no un cambio rompedor.

Qué **no** cubre, y es una limitación real, no un olvido: `@background_task`. Su payload
**es** el dict de kwargs que el worker le pasa a ``task_func(**payload)``, así que meterle
una clave ``__meta__`` colisionaría con un parámetro de la tarea y rompería la llamada.
Además no hay objeto-mensaje al que atar el grant. Una tarea que necesita saber quién la
originó recibe ese dato como **parámetro explícito**.

Uso (desde un transporte)::

    payload = self._serializer.serialize_envelope(command)
    await self._enqueuer.enqueue_command(name, payload, queue="default")

Uso (desde un consumidor)::

    message, metadata = self._serializer.deserialize_envelope(payload)
    async with restored_envelope_scope(metadata, message):
        await self._command_bus.dispatch(message)
"""
from __future__ import annotations

import abc
import threading
import typing as t
from contextlib import AsyncExitStack, asynccontextmanager

__all__ = [
    "ENVELOPE_METADATA_KEY",
    "EnvelopeMetadataProvider",
    "AbstractEnvelopeRestorer",
    "register_envelope_metadata_provider",
    "register_envelope_restorer",
    "unregister_envelope_key",
    "clear_envelope_registry",
    "registered_envelope_keys",
    "collect_envelope_metadata",
    "restored_envelope_scope",
    "message_correlation_id",
]

#: La clave que el sobre agrega al payload serializado.
#:
#: Con doble underscore a los dos lados, igual que `__type__` y `__data__`: los tres son
#: metadata del transporte y no campos del mensaje, y el prefijo los mantiene fuera de
#: cualquier colisión con un nombre de campo del consumidor.
ENVELOPE_METADATA_KEY = "__meta__"

#: Un proveedor recibe el mensaje y devuelve el valor a poner bajo su clave, o `None` para
#: no aportar nada. Recibe el mensaje —y no sólo el contexto ambiental— porque el grant se
#: ata al mensaje: ver `AbstractEnvelopeRestorer`.
EnvelopeMetadataProvider = t.Callable[[t.Any], t.Any]


class AbstractEnvelopeRestorer(abc.ABC):
    """
    Restaura el contexto ambiental que un valor del sobre representa.

    `restore` es un context manager **asíncrono** y no una función que devuelve un valor, y
    los dos detalles importan:

    - **Context manager**, porque restaurar contexto ambiental es un set/reset de
      `ContextVar` y el reset tiene que ocurrir aunque el handler lance. Devolver un valor
      dejaría el reset en manos de quien llama, que es exactamente cómo se filtra contexto
      entre jobs de un mismo worker.
    - **Asíncrono**, porque una restauración seria valida contra el almacén. Verificar la
      firma y el vencimiento no alcanza: entre el encolado y la ejecución la sesión pudo
      revocarse, y un sobre con TTL de 24 h sin ese chequeo son 24 h de ejecución con una
      credencial revocada.

    Un restaurador **falla cerrando**: si el valor no verifica, lanza. Ejecutar el mensaje
    sin contexto sería peor que no ejecutarlo, porque el handler correría con la autoridad
    ambiental que hubiera quedado del job anterior.
    """

    @abc.abstractmethod
    def restore(
        self, value: t.Any, message: t.Any
    ) -> t.AsyncContextManager[None]:
        """
        Devuelve el scope dentro del cual el contexto está restaurado.

        Args:
            value: Lo que el proveedor puso bajo esta clave.
            message: El mensaje ya deserializado. Se pasa para poder verificar que el valor
                venía atado **a este** mensaje: sin ese chequeo, un sobre capturado de un
                "borrar cuenta" se re-adjunta a un "transferir fondos", verifica —está bien
                firmado— y el worker ejecuta la transferencia con la autoridad del grant de
                borrado. Es escalación de privilegios a un `LPUSH` de distancia.
        """
        raise NotImplementedError


# ── Registro ──────────────────────────────────────────────────────────────────
# Global de proceso con `RLock`, igual que `_container` de CQRS y de Darwin: el cableado del
# sobre es una propiedad del proceso, no de una instancia de bus, porque los cinco
# transportes tienen que sellar igual sin que nadie los configure de a uno.
_providers: dict[str, EnvelopeMetadataProvider] = {}
_restorers: dict[str, AbstractEnvelopeRestorer] = {}
_lock = threading.RLock()

_SIN_RESTAURADOR = (
    "Llegó un mensaje con un sobre bajo la clave '{key}' y este proceso no tiene un "
    "restaurador registrado para esa clave.\n\n"
    "No se ejecuta a propósito: el productor selló un contexto que este worker no puede "
    "verificar, así que el handler correría sin la autoridad que el mensaje traía —o, peor, "
    "con la que quedó del job anterior.\n\n"
    "Si la clave es 'auth', al worker le falta el cableado de Darwin:\n\n"
    "    from hexcore.darwin import IdentityConfig, configure_identity\n\n"
    "    configure_identity(IdentityConfig())   # en el arranque del worker, no sólo del web\n"
)


def register_envelope_metadata_provider(
    key: str, provider: EnvelopeMetadataProvider
) -> None:
    """
    Registra el proveedor de la clave `key`. Reemplaza al anterior si había.

    Reemplaza en vez de acumular: dos proveedores para la misma clave sólo pueden significar
    que alguien se cableó dos veces, y elegir uno de los dos en silencio da un sobre que
    depende del orden de importación.
    """
    with _lock:
        _providers[key] = provider


def register_envelope_restorer(key: str, restorer: AbstractEnvelopeRestorer) -> None:
    """Registra el restaurador de la clave `key`. Reemplaza al anterior si había."""
    with _lock:
        _restorers[key] = restorer


def unregister_envelope_key(key: str) -> None:
    """Saca el proveedor y el restaurador de `key`. Idempotente."""
    with _lock:
        _providers.pop(key, None)
        _restorers.pop(key, None)


def clear_envelope_registry() -> None:
    """Vacía el registro. Para tests: sin esto, un test filtra su cableado al siguiente."""
    with _lock:
        _providers.clear()
        _restorers.clear()


def registered_envelope_keys() -> frozenset[str]:
    """Las claves con proveedor o restaurador. Para diagnóstico y tests."""
    with _lock:
        return frozenset(_providers) | frozenset(_restorers)


# ── Sellado ───────────────────────────────────────────────────────────────────
def collect_envelope_metadata(message: t.Any) -> dict[str, t.Any]:
    """
    Consulta a todos los proveedores y arma el sobre para `message`.

    Un proveedor que devuelve `None` no aporta la clave — es el caso normal cuando no hay
    contexto ambiental que propagar (un cron sin `system_context`, un comando despachado
    desde un script).

    Un proveedor que **lanza** propaga: el encolado falla. Es deliberado y es el criterio
    inverso al de `rate_limit`. Tragarse el error dejaría un mensaje encolado sin el actor,
    que después se ejecuta sin autoridad y falla en el worker con un error que no señala la
    causa — o, si el handler no chequea nada, se ejecuta como si nadie lo hubiera pedido.
    """
    with _lock:
        proveedores = tuple(_providers.items())

    sobre: dict[str, t.Any] = {}
    for key, provider in proveedores:
        valor = provider(message)
        if valor is not None:
            sobre[key] = valor
    return sobre


# ── Restauración ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def restored_envelope_scope(
    metadata: t.Mapping[str, t.Any] | None, message: t.Any
) -> t.AsyncIterator[None]:
    """
    Entra en el scope de cada restaurador que las claves de `metadata` nombren.

    Sin metadata no hace nada y no cuesta nada: es el camino de todo mensaje que se encoló
    antes de que este mecanismo existiera, y el de todo proceso que no cablea ningún
    proveedor.

    Raises:
        RuntimeError: si una clave del sobre no tiene restaurador registrado, con la línea
            de cableado que falta.

    Uso::

        message, metadata = serializer.deserialize_envelope(payload)
        async with restored_envelope_scope(metadata, message):
            await command_bus.dispatch(message)
    """
    if not metadata:
        yield
        return

    with _lock:
        restauradores = dict(_restorers)

    async with AsyncExitStack() as stack:
        for key, valor in metadata.items():
            restorer = restauradores.get(key)
            if restorer is None:
                raise RuntimeError(_SIN_RESTAURADOR.format(key=key))
            await stack.enter_async_context(restorer.restore(valor, message))
        yield


# ── Utilidad compartida ───────────────────────────────────────────────────────
def message_correlation_id(message: t.Any) -> str | None:
    """
    El identificador propio del mensaje, como string, o `None` si no tiene.

    Vive acá y no en Darwin porque es conocimiento del núcleo: un `Command` lleva
    `command_id` y un `DomainEvent` lleva `event_id`. Un restaurador lo usa para atar el
    valor del sobre a **este** mensaje, y necesita calcularlo igual en las dos puntas.
    """
    for atributo in ("command_id", "event_id"):
        valor = getattr(message, atributo, None)
        if valor is not None:
            return str(valor)
    return None
