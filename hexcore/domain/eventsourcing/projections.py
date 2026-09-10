"""
Proyecciones: modelos de lectura alimentados por el orden global del almacen.

Una proyeccion es el otro lado de CQRS. Los agregados responden "que puede pasar ahora",
leyendo un stream; las proyecciones responden "que hay", leyendo todos. Se reconstruyen desde
cero cuando hace falta, y es esa reconstruibilidad —y no una migracion— lo que permite
cambiar la forma de un modelo de lectura.
"""
from __future__ import annotations

import abc
import typing as t

from hexcore.domain.events import DomainEvent

from .stored import StoredEvent

__all__ = ["AbstractProjection", "AbstractCheckpointStore"]


class AbstractProjection(abc.ABC):
    """
    Un modelo de lectura que se actualiza evento a evento.

    **Tiene que ser idempotente.** No es una recomendacion de estilo: la entrega es
    at-least-once por construccion (ver `AbstractEventStore.read_all`), asi que el mismo
    evento va a llegar dos veces mas temprano que tarde — en un reinicio, tras un fallo, o
    releyendo la ventana de seguridad. Un `contador += 1` sin proteccion se descuadra solo.
    Lo que funciona es escribir por clave (un upsert) o deduplicar por `event_id`.
    """

    #: Identifica la proyeccion en los logs y en los errores. Obligatorio.
    name: t.ClassVar[str]

    #: Los tipos que le interesan. **Vacio significa todos**, no ninguno: es el default
    #: porque una proyeccion que filtra de mas se queda callada y desactualizada, mientras
    #: que una que recibe de mas solo desperdicia una llamada.
    #:
    #: El filtro es por jerarquia, con la misma `handlers_for` que usan los buses: declarar
    #: `handles = (PedidoEvent,)` alcanza a todas sus subclases.
    handles: t.ClassVar[tuple[type[DomainEvent], ...]] = ()

    @abc.abstractmethod
    async def apply(self, event: DomainEvent, stored: StoredEvent) -> None:
        """
        Aplica un evento al modelo de lectura.

        Recibe el `StoredEvent` **ademas** del evento porque el evento de dominio no conoce
        su propio `stream_id` ni su `global_position`, y una proyeccion casi siempre necesita
        al menos el primero para saber a que fila escribir. Va como argumento aparte y no
        pegado al evento porque los eventos son `frozen`, que es la inmutabilidad que el
        resto del framework ya eligio.
        """
        raise NotImplementedError

    async def reset(self) -> None:
        """
        Deja el modelo de lectura vacio. Lo llama `Projector.rebuild()` antes de reprocesar.

        El default es no-op para que una proyeccion sin estado propio no tenga que
        implementarlo. Pero **una proyeccion que escribe y no implementa `reset()` produce un
        rebuild incorrecto**: los datos viejos quedan mezclados con los nuevos y el resultado
        no es reproducible. Si tu proyeccion escribe algo, implementalo.
        """
        return None


class AbstractCheckpointStore(abc.ABC):
    """
    Recuerda hasta que `global_position` leyo cada suscripcion.

    Es lo que hace que un proyector reiniciado siga donde estaba en vez de reprocesar el
    almacen entero. La clave es el nombre de la suscripcion — no de la proyeccion: varias
    proyecciones que avanzan juntas comparten checkpoint, y ese agrupamiento es lo que
    mantiene consistentes entre si a los modelos de lectura que se leen juntos.
    """

    @abc.abstractmethod
    async def load(self, subscription: str) -> int:
        """La ultima posicion procesada, o **0** si la suscripcion es nueva."""
        raise NotImplementedError

    @abc.abstractmethod
    async def save(self, subscription: str, position: int) -> None:
        """
        Guarda la posicion.

        Se llama **despues** de aplicar el lote, nunca antes. Al reves, una caida entre el
        guardado y la aplicacion saltearia eventos para siempre: al reiniciar, el checkpoint
        diria que ya se procesaron. Guardando despues, una caida los reprocesa — que es
        justo lo que la idempotencia de las proyecciones cubre.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def reset(self, subscription: str) -> None:
        """Vuelve la suscripcion a 0, para reconstruir desde el principio."""
        raise NotImplementedError
