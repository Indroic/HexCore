"""
Excepciones del Event Sourcing.

**No heredan de `CQRSError`.** El event store es util sin CQRS — es un almacen de hechos,
no un bus de mensajes — y colgarlo de esa jerarquia tendria una consecuencia concreta: un
`except CQRSError` del consumidor, escrito para tolerar que no haya handler registrado, se
comeria un conflicto de concurrencia. Y un `ConcurrencyError` que se traga es una escritura
perdida sin rastro.
"""
from __future__ import annotations


class EventSourcingError(Exception):
    """Base de todas las excepciones del event store."""


class ConcurrencyError(EventSourcingError):
    """
    Otro escritor avanzo el stream entre la lectura y la escritura.

    Es la excepcion **esperada** del camino optimista, no un fallo del sistema: significa que
    el agregado que tenias en memoria ya no refleja el estado del stream. Lo correcto es
    recargarlo y reintentar la operacion de negocio.

    `EventSourcedRepository.save()` la deja escapar **sin tocar los eventos pendientes** del
    agregado, justamente para que el reintento sea posible.
    """

    def __init__(self, stream_id: str, expected: int, actual: int) -> None:
        self.stream_id = stream_id
        self.expected_version = expected
        self.actual_version = actual
        super().__init__(
            f"El stream '{stream_id}' esta en la version {actual}, pero se esperaba "
            f"{expected}. Otro escritor lo avanzo: recarga el agregado y reintenta."
        )


class AggregateNotFoundError(EventSourcingError):
    """
    No hay ningun evento para ese stream.

    Un stream vacio y un stream inexistente son lo mismo en un event store: el estado de un
    agregado *es* su historial, asi que sin eventos no hay agregado.
    """

    def __init__(self, aggregate_type: str, stream_id: str) -> None:
        self.aggregate_type = aggregate_type
        self.stream_id = stream_id
        super().__init__(
            f"No hay eventos para el stream '{stream_id}': el agregado "
            f"'{aggregate_type}' no existe."
        )


class UnhandledEventError(EventSourcingError):
    """
    Un evento del historial que el agregado no sabe aplicar.

    Falla en vez de ignorarlo porque la alternativa es peor de lo que parece: un evento sin
    mutador durante un replay no es un no-op, es **estado que se pierde en silencio**. El
    agregado queda reconstruido a medias, las decisiones de negocio se toman sobre ese estado
    incompleto, y el error se manifiesta mucho despues y muy lejos. Ese es el modo de falla
    que vuelve irrecuperable un event store.

    Para el caso legitimo —un evento que solo le interesa a las proyecciones— esta
    `__strict_mutators__ = False` en el agregado, que es una decision explicita y por clase.
    """

    def __init__(self, aggregate_type: str, event_type: str) -> None:
        self.aggregate_type = aggregate_type
        self.event_type = event_type
        super().__init__(
            f"'{aggregate_type}' no tiene un mutador para '{event_type}'. Durante un replay "
            f"eso es estado que se pierde sin aviso. Declara un metodo con "
            f"@when({event_type}) o, si el evento solo le importa a las proyecciones, pone "
            f"__strict_mutators__ = False en el agregado."
        )
