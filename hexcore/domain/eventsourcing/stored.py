"""
`StoredEvent`: un evento tal como vive en el almacen.

Un `DomainEvent` describe **que** paso. Un `StoredEvent` agrega el resto de lo que hace falta
para poder releer el pasado: en que stream, en que orden dentro de ese stream, en que orden
respecto de todos los demas, y con que contexto se produjo.
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EXPECTED_VERSION_ANY",
    "EXPECTED_VERSION_NO_STREAM",
    "StoredEvent",
]

#: "No me importa en que version este el stream": el append se hace al final, sin verificar.
#: Es lo que usa el camino de *event log* — el UoW persistiendo los eventos de entidades
#: clasicas—, donde no hay un agregado que haya leido una version y quiera defenderla.
EXPECTED_VERSION_ANY: t.Final[int] = -1

#: "El stream tiene que no existir todavia": es la version que lleva un agregado recien
#: creado, y lo que convierte a la creacion en una operacion segura frente a concurrencia.
#: Dos procesos que creen el mismo id a la vez: uno gana, el otro recibe `ConcurrencyError`.
EXPECTED_VERSION_NO_STREAM: t.Final[int] = 0


class StoredEvent(BaseModel):
    """
    Un evento persistido, con su posicion en el stream y en el orden global.

    Inmutable, como el hecho que representa. Un event store es append-only: corregir el
    pasado se hace agregando un evento compensatorio, nunca editando uno.
    """

    #: Identidad del stream, `f"{stream_type}-{aggregate_id}"` para un agregado.
    #:
    #: Es `str` y no `UUID` a proposito: no todo stream es un agregado. Un stream de
    #: integracion, una saga o una categoria tienen identidades que no son UUIDs, y en Redis
    #: el `stream_id` es literalmente parte del nombre de la clave.
    stream_id: str

    #: La categoria del stream (`"pedido"`, `"usuario"`). Es lo que permite leer el orden
    #: global filtrando por tipo sin tener que parsear el `stream_id`.
    stream_type: str

    #: Posicion dentro del stream, **1-based**. Unica por `(stream_id, version)`, y esa
    #: unicidad —no un `SELECT MAX(version)`— es la garantia real de la concurrencia
    #: optimista. Arranca en 1 para que 0 pueda significar "el stream no existe".
    version: int

    #: Posicion en el orden global del almacen, **1-based**. La asigna el store, nunca el
    #: llamador. 0 queda reservado para "no se leyo nada todavia" en un checkpoint.
    #:
    #: **Es monotonica pero no necesariamente contigua.** Ver el docstring de
    #: `AbstractEventStore.read_all` para por que, y que hacer al respecto.
    global_position: int

    #: El `event_id` del `DomainEvent` original. Es la clave de idempotencia: el relay y las
    #: proyecciones deduplican por aca cuando reprocesan un tramo.
    event_id: UUID

    #: El FQN del tipo (`"app.dominio.PedidoPagado"`), derivado de `build_fqn()`.
    #:
    #: **No es `DomainEvent.event_name`.** `event_name` no es unico entre modulos, es una
    #: etiqueta legible pensada para el ruteo de AMQP y para los logs, y ademas viaja dentro
    #: del payload como campo calculado. El FQN es lo que el serializador sabe resolver.
    #: Esta columna esta duplicada respecto de `payload["__type__"]` solo para poder
    #: indexarla.
    event_type: str

    #: El sobre completo de `AbstractSerializer.serialize_envelope()`, o sea
    #: `{"__type__", "__data__"}` y, si hay proveedores registrados, `"__meta__"`.
    #:
    #: Se guarda el sobre entero y no solo los datos porque el `__meta__` es el contexto
    #: ambiental —quien estaba autenticado, el `request_id`— que Darwin sella al publicar.
    #: Tirarlo perderia la unica evidencia de *quien* causo el hecho, que es justo lo que un
    #: log de eventos existe para conservar. Y conservarlo hace que republicar sea reconstruir
    #: el sobre y publicarlo, con el contexto intacto: por eso esta tabla puede ser el outbox
    #: y no hace falta una segunda.
    payload: dict[str, t.Any] = Field(default_factory=dict)

    #: Cuando ocurrio el hecho, segun el `DomainEvent`.
    occurred_on: datetime

    #: Cuando se escribio en el almacen. Distinto de `occurred_on` cuando un evento se
    #: reprocesa o se importa, y es la diferencia que hace legible una migracion.
    recorded_at: datetime

    model_config = ConfigDict(frozen=True)
