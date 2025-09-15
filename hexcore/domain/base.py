from __future__ import annotations
import typing as t
import abc
from pydantic import BaseModel, Field, ConfigDict
from uuid import UUID, uuid4
from datetime import datetime, UTC

if t.TYPE_CHECKING:
    from hexcore.domain.events import DomainEvent


class BaseEntity(BaseModel):
    """
    Clase base para todas las entidades del dominio.

    Proporciona campos comunes y configuración de Pydantic para asegurar consistencia
    y comportamiento predecible en todo el modelo.

    Atributos:
        id (UUID): Identificador único universal para la entidad.
        created_at (datetime): Marca de tiempo de la creación de la entidad (UTC).
        updated_at (datetime): Marca de tiempo de la última actualización (UTC).
        is_active (bool): Indicador para borrado lógico (soft delete).
    """

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_active: t.Optional[bool] = True

    _domain_events: t.List[DomainEvent] = []

    model_config = ConfigDict(
        from_attributes=True,  # Permite crear modelos desde atributos de objetos (clave para ORMs).
        validate_assignment=True,  # Vuelve a validar la entidad cada vez que se modifica un campo.
        # `frozen=True` hace que las entidades sean inmutables, lo cual es un ideal de DDD.
        # Sin embargo, puede complicar el manejo de estado con un ORM como SQLAlchemy,
        # donde los objetos a menudo se modifican y luego se guardan.
        # Lo cambiamos a False para un enfoque más pragmático.
        frozen=False,
    )

    def register_event(self, event: DomainEvent) -> None:
        """Añade un evento a la lista de la entidad."""
        self._domain_events.append(event)

    def pull_domain_events(self) -> t.List[DomainEvent]:
        """Entrega los eventos y limpia la lista."""
        events = self._domain_events[:]
        self._domain_events.clear()
        return events

    def clear_domain_events(self) -> None:
        """Limpia la lista de eventos sin entregarlos."""
        self._domain_events.clear()

    async def deactivate(self) -> None:
        """Desactiva la Entidad(Borrado Logico)"""
        self.is_active = False


class AbstractModelMeta(BaseEntity, abc.ABC):
    """
    Metaclase para resolver un conflicto entre Pydantic y las clases abstractas de Python.

    Problema:
        - Pydantic (`BaseModel`) usa su propia metaclase para la validación de datos.
        - Las clases abstractas de Python (`abc.ABC`) usan `abc.ABCMeta` para permitir `@abstractmethod`.
        - Una clase no puede tener dos metaclases diferentes.

    Solución:
        Esta metaclase combina ambas, permitiendo crear clases que son a la vez
        modelos de Pydantic y clases base abstractas.

    Uso:
        class MiClaseAbstracta(BaseEntity, abc.ABC, metaclass=AbstractModelMeta):
            ...
    """

    pass
