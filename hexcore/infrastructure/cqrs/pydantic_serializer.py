"""
Serializer basado en Pydantic (default) para transportar Commands/Events
a través de backends asíncronos (Procrastinate, Celery, etc.).
"""
from __future__ import annotations

import importlib
import typing as t

from hexcore.domain.cqrs.serializer import ISerializer
from hexcore.domain.cqrs.exceptions import DeserializationError


class PydanticSerializer(ISerializer):
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
        fqn = f"{type(message).__module__}.{type(message).__qualname__}"
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
            module_path, class_name = fqn.rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except (ValueError, ModuleNotFoundError, AttributeError) as exc:
            raise DeserializationError(
                f"Cannot resolve type '{fqn}': {exc}"
            ) from exc

        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise DeserializationError(
                f"Failed to validate data for '{fqn}': {exc}"
            ) from exc
