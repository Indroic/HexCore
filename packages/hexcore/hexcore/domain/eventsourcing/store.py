"""
`AbstractEventStore`: el puerto del almacen de eventos.
"""
from __future__ import annotations

import abc
import typing as t

from hexcore.domain.events import DomainEvent

from .stored import StoredEvent

if t.TYPE_CHECKING:
    from hexcore.domain.cqrs.serializer import AbstractSerializer

__all__ = ["AbstractEventStore"]


class AbstractEventStore(abc.ABC):
    """
    Almacen append-only de eventos de dominio, ordenados por stream y globalmente.

    Un adaptador implementa los cuatro metodos abstractos. Los concretos de mas abajo se
    apoyan en ellos y en el serializador.

    El serializador **tiene que ser el mismo que usa el bus de CQRS**. Si divergen, un evento
    escrito por el store y otro publicado por el bus no tienen el mismo formato, y el dia que
    haya que releer el historial con el consumidor del bus el payload no se va a poder
    reconstruir. `EventStoreFactory` lo comparte por eso.
    """

    def __init__(self, serializer: "AbstractSerializer | None" = None) -> None:
        self._serializer = serializer or self._serializador_por_defecto()

    @staticmethod
    def _serializador_por_defecto() -> "AbstractSerializer":
        """
        `PydanticSerializer`, importado tarde.

        El import es local porque vive en `infrastructure/` y este modulo es dominio puro:
        hacerlo arriba invertiria la dependencia y ataria el puerto a su adaptador.
        """
        from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer

        return PydanticSerializer()

    @property
    def serializer(self) -> "AbstractSerializer":
        """El serializador en uso. Lo necesita el relay para no reconstruir el sobre a mano."""
        return self._serializer

    # ── Escritura ─────────────────────────────────────────────────────────────
    @abc.abstractmethod
    async def append(
        self,
        stream_id: str,
        events: t.Sequence[DomainEvent],
        *,
        expected_version: int,
        stream_type: str | None = None,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> list[StoredEvent]:
        """
        Agrega eventos al final del stream, de forma atomica.

        Args:
            stream_id: El stream al que se agregan.
            events: Los eventos, en orden. Una lista vacia es un **no-op**: devuelve `[]` y
                **no valida `expected_version`**. Guardar un agregado que no cambio no tiene
                por que fallar por concurrencia.
            expected_version: La version que el llamador cree que tiene el stream.
                `EXPECTED_VERSION_ANY` saltea la comprobacion;
                `EXPECTED_VERSION_NO_STREAM` exige que el stream no exista.
            stream_type: La categoria. Si no se pasa, se deriva del `stream_id`.
            metadata: Sobre a sellar en el payload. Por defecto lo arma el serializador con
                los proveedores registrados, que es lo que hace viajar al actor.

        Returns:
            Los eventos persistidos, con `version` y `global_position` ya asignadas.

        Raises:
            ConcurrencyError: Si la version del stream no es `expected_version`.
        """
        raise NotImplementedError

    # ── Lectura ───────────────────────────────────────────────────────────────
    @abc.abstractmethod
    async def read_stream(
        self,
        stream_id: str,
        *,
        from_version: int = 1,
        to_version: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        """
        El historial de un stream, ordenado por `version` ascendente.

        `from_version` y `to_version` son **inclusivos**: con un snapshot en la version 40,
        la cola pendiente se pide con `from_version=41`. Un stream inexistente devuelve `[]`,
        no lanza: distinguir "no existe" de "existe y esta vacio" no aporta nada, porque en
        un event store son lo mismo.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def read_all(
        self,
        *,
        from_position: int = 0,
        limit: int = 500,
        stream_types: t.Sequence[str] | None = None,
    ) -> list[StoredEvent]:
        """
        El orden global, para el relay y las proyecciones.

        `from_position` es **exclusivo** (estrictamente mayor), asi un checkpoint se pasa tal
        cual sin sumarle uno. El default 0 lee desde el principio, porque las posiciones
        arrancan en 1.

        **La `global_position` es monotonica pero no contigua, y eso importa.** En un backend
        SQL la asigna una secuencia, que se toma al insertar y no al comitear: la transaccion
        que reservo la 10 puede comitear despues de la que reservo la 11. Un lector que hace
        `WHERE global_position > checkpoint` puede entonces pasar por encima de la 10 y no
        verla nunca. En Mongo pasa algo parecido con el contador: una transaccion abortada
        quema la posicion que reservo.

        La consecuencia es que la entrega es **at-least-once, no exactly-once**, y que un
        consumidor tiene que ser idempotente. `Projector` y `EventStoreRelay` lo compensan
        releyendo una ventana hacia atras (`safety_window`) y deduplicando por `event_id`.
        Quien necesite orden estricto tiene `ordering="serialized"` en el adaptador de SQL,
        que serializa las escrituras a cambio de rendimiento.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def stream_version(self, stream_id: str) -> int:
        """La version actual del stream, o **0** si no existe."""
        raise NotImplementedError

    # ── Concretos ─────────────────────────────────────────────────────────────
    # Concretos y no abstractos, por el mismo criterio que documenta
    # `AbstractSerializer.serialize_envelope`: sumar un @abstractmethod al puerto rompe a
    # todo el que ya tenga un store propio, y estos tres no lo necesitan porque solo
    # envuelven a los cuatro de arriba y al serializador. Un adaptador con un formato
    # particular puede sobreescribirlos; ninguno esta obligado.

    def rehydrate(self, stored: StoredEvent) -> DomainEvent:
        """
        Reconstruye el `DomainEvent` desde el payload.

        Raises:
            DeserializationError: Si el tipo ya no se puede resolver — la clase se renombro
                o se borro. Es un caso real en un almacen que guarda anos de historia, y por
                eso el error nombra el tipo: sin eso, un rebuild que falla no dice sobre que.
        """
        evento, _ = self.rehydrate_with_metadata(stored)
        return evento

    def rehydrate_with_metadata(
        self, stored: StoredEvent
    ) -> tuple[DomainEvent, dict[str, t.Any]]:
        """
        El evento y su sobre, por separado.

        Es lo que usa el relay: republicar sin el sobre perderia el actor y el `request_id`
        que el productor original sello, y los handlers que dependen de ese contexto verian
        un evento anonimo.
        """
        evento, sobre = self._serializer.deserialize_envelope(dict(stored.payload))
        return t.cast(DomainEvent, evento), sobre

    async def stream_exists(self, stream_id: str) -> bool:
        """Si el stream tiene al menos un evento."""
        return await self.stream_version(stream_id) > 0
