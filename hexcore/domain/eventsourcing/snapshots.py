"""
Snapshots: el estado de un agregado en una version, para no releer el stream entero.

Es una **optimizacion de lectura y nada mas**. El historial sigue siendo la fuente de verdad,
y un almacen sin snapshots da exactamente los mismos resultados, solo que mas lento. Esa
propiedad es la que permite que un fallo al guardar un snapshot se loguee en vez de
propagarse: perder una escritura porque no se pudo cachear seria absurdo.
"""
from __future__ import annotations

import abc
import typing as t
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Snapshot", "AbstractSnapshotStore"]


class Snapshot(BaseModel):
    """El estado serializado de un agregado tal como estaba en `version`."""

    stream_id: str

    #: El FQN del agregado. Se guarda para poder detectar el caso en que un `stream_id` se
    #: reusa para otro tipo: restaurar un `Pedido` desde el snapshot de un `Usuario` daria un
    #: objeto valido para pydantic y basura para el negocio.
    aggregate_type: str

    #: La version del stream que este estado refleja. La lectura sigue desde `version + 1`.
    version: int

    #: `aggregate.snapshot_state()`, o sea `model_dump(mode="json")`.
    state: dict[str, t.Any] = Field(default_factory=dict)

    taken_at: datetime

    model_config = ConfigDict(frozen=True)


class AbstractSnapshotStore(abc.ABC):
    """
    Puerto del almacen de snapshots.

    **Guarda historial, no pisa.** Un snapshot corrupto —por un bug en `snapshot_state()`, o
    por un campo que cambio de forma— con el historial intacto se recupera borrandolo: el
    agregado vuelve a leerse desde el evento 1. Pisando el anterior, el unico estado bueno se
    perderia y la unica salida seria borrar igual, pero habiendo perdido tambien los
    intermedios. Guardar sale barato y compra la posibilidad de retroceder.
    """

    @abc.abstractmethod
    async def load(
        self, stream_id: str, *, max_version: int | None = None
    ) -> Snapshot | None:
        """
        El snapshot **mas reciente** del stream, o `None` si no hay ninguno.

        `max_version` acota la busqueda al mas reciente que no supere esa version. Es lo que
        hace posible reconstruir el agregado tal como estaba en un punto del pasado, que es
        la operacion con la que se depura un bug de dominio.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def save(self, snapshot: Snapshot) -> None:
        """Guarda un snapshot. Guardar dos veces la misma version es idempotente."""
        raise NotImplementedError

    @abc.abstractmethod
    async def delete(self, stream_id: str) -> None:
        """
        Borra **todos** los snapshots del stream.

        Es la operacion de recuperacion: despues de esto el agregado se reconstruye desde el
        evento 1, que siempre es correcto porque el historial no se toco.
        """
        raise NotImplementedError
