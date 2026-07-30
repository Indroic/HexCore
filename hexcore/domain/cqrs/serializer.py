"""
Contrato para serialización/deserialización de mensajes CQRS.
Requerido por buses asíncronos que necesitan persistir mensajes (ej. Procrastinate, Celery).
"""
from __future__ import annotations

import abc
import typing as t


class AbstractSerializer(abc.ABC):
    """
    Puerto para serializar/deserializar mensajes CQRS en formatos transportables.
    """

    @abc.abstractmethod
    def serialize(self, message: t.Any) -> dict[str, t.Any]:
        """
        Serializa un mensaje (Command/Query/Event) a un diccionario
        transportable por el backend.

        El dict DEBE incluir metadata suficiente para reconstruir el tipo
        original (ej. ``'__type__': 'module.ClassName'``).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def deserialize(self, data: dict[str, t.Any]) -> t.Any:
        """
        Reconstruye un mensaje a partir de su representación serializada.

        Raises:
            DeserializationError: Si no se puede reconstruir el mensaje.
        """
        raise NotImplementedError


# ── Alias de retrocompatibilidad (deprecado desde 5.0) ────────────────────────
from hexcore._deprecation import deprecated_aliases  # noqa: E402

_DEPRECATED_ALIASES = {"ISerializer": "AbstractSerializer"}

__getattr__ = deprecated_aliases(__name__, _DEPRECATED_ALIASES, globals())

if t.TYPE_CHECKING:
    ISerializer = AbstractSerializer
