"""
Excepciones específicas de la capa CQRS.
"""


class CQRSError(Exception):
    """Base para todas las excepciones CQRS."""


class HandlerNotFoundError(CQRSError):
    """No se encontró un handler registrado para el mensaje dado."""

    def __init__(self, message_type: type) -> None:
        self.message_type = message_type
        super().__init__(
            f"No handler registered for {message_type.__module__}.{message_type.__qualname__}"
        )


class DuplicateHandlerError(CQRSError):
    """Ya existe un handler registrado para este tipo de mensaje."""

    def __init__(self, message_type: type) -> None:
        self.message_type = message_type
        super().__init__(
            f"Handler already registered for {message_type.__module__}.{message_type.__qualname__}"
        )


class DeserializationError(CQRSError):
    """Error al deserializar un mensaje desde el backend."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"Failed to deserialize message: {detail}")
