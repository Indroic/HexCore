"""
Serializer basado en Pydantic (default) para transportar Commands/Events
a través de backends asíncronos (Procrastinate, Celery, etc.).
"""
from __future__ import annotations

import typing as t

from hexcore.domain.cqrs.serializer import AbstractSerializer
from hexcore.domain.cqrs.exceptions import DeserializationError
from hexcore.domain.cqrs.resolution import build_fqn, resolve_dotted


class PydanticSerializer(AbstractSerializer):
    """
    Serializa/deserializa mensajes Pydantic BaseModel.
    Almacena el fully-qualified type name para reconstrucción.

    Formato::

        {
            "__type__": "myapp.commands.CreateUserCommand",
            "__data__": { ...model_dump()... }
        }
    """

    def serialize(self, message: t.Any) -> dict[str, t.Any]:
        fqn = build_fqn(type(message))
        return {
            "__type__": fqn,
            "__data__": message.model_dump(mode="json"),
        }

    def deserialize(self, data: dict[str, t.Any]) -> t.Any:
        fqn = data.get("__type__")
        raw = data.get("__data__")
        if not fqn or raw is None:
            raise DeserializationError(
                f"Missing '__type__' or '__data__' in payload: {data!r}"
            )

        try:
            # `resolve_dotted` soporta clases anidadas ("app.Outer.InnerCommand"),
            # donde un rsplit(".", 1) produciría un module path inválido.
            cls = resolve_dotted(fqn)
        except LookupError as exc:
            raise DeserializationError(
                f"Cannot resolve type '{fqn}': {exc}"
            ) from exc

        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise DeserializationError(
                f"Failed to validate data for '{fqn}': {exc}"
            ) from exc
