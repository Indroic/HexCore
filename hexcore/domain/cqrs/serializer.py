"""
Contrato para serialización/deserialización de mensajes CQRS.
Requerido por buses asíncronos que necesitan persistir mensajes (ej. Procrastinate, Celery).
"""
from __future__ import annotations

import abc
import typing as t

from hexcore.domain.cqrs.envelope import (
    ENVELOPE_METADATA_KEY,
    collect_envelope_metadata,
)

__all__ = ["AbstractSerializer"]


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

    # ── El sobre ──────────────────────────────────────────────────────────────
    # Los dos métodos que siguen son **concretos**, y no abstractos, a propósito: agregar un
    # `@abc.abstractmethod` al puerto rompería a todo el que tenga un serializador propio, y
    # el mecanismo del sobre no lo necesita —lo único que hace es envolver `serialize` y
    # `deserialize`, que sí son abstractos. Un serializador con un formato particular puede
    # sobreescribirlos; ninguno está obligado.

    def serialize_envelope(
        self, message: t.Any, metadata: t.Mapping[str, t.Any] | None = None
    ) -> dict[str, t.Any]:
        """
        Serializa `message` y le agrega el sobre de metadata ambiental.

        Es lo que llaman los transportes en lugar de `serialize()`. Sin proveedores
        registrados el sobre queda vacío y **no se agrega la clave**, así que el payload es
        idéntico al de antes: es lo que hace que el mecanismo sea aditivo.

        Args:
            message: El mensaje a serializar.
            metadata: El sobre a usar. Por defecto se consulta el registro de proveedores
                (`collect_envelope_metadata`); pasarlo explícito es para tests y para un
                productor que quiera sellar algo puntual.

        Uso::

            payload = serializer.serialize_envelope(comando)
            await enqueuer.enqueue_command("CrearTicket", payload, queue="default")
        """
        payload = self.serialize(message)
        if metadata is None:
            metadata = collect_envelope_metadata(message)
        if metadata:
            payload[ENVELOPE_METADATA_KEY] = dict(metadata)
        return payload

    def deserialize_envelope(
        self, data: dict[str, t.Any]
    ) -> tuple[t.Any, dict[str, t.Any]]:
        """
        Reconstruye el mensaje y devuelve el sobre aparte.

        Un payload **sin** sobre se deserializa igual y devuelve un sobre vacío. Ése es el
        requisito que fija el diseño: los mensajes que ya estaban encolados cuando esto se
        deployó tienen que seguir procesándose, y un worker nuevo consumiendo una cola vieja
        es el caso normal de todo deploy.

        La clave del sobre se **saca** antes de delegar en `deserialize()`, en vez de confiar
        en que la ignore: un serializador estricto que valide las claves del payload
        fallaría, y ese serializador es legítimo.

        Returns:
            El mensaje y el sobre.
        """
        crudo: t.Any = data.get(ENVELOPE_METADATA_KEY)
        metadata: dict[str, t.Any] = (
            dict(t.cast("dict[str, t.Any]", crudo)) if isinstance(crudo, dict) else {}
        )
        if ENVELOPE_METADATA_KEY in data:
            data = {k: v for k, v in data.items() if k != ENVELOPE_METADATA_KEY}
        return self.deserialize(data), metadata
