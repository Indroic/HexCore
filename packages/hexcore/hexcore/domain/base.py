from __future__ import annotations
import typing as t
import abc
from pydantic import BaseModel, Field, ConfigDict, PrivateAttr
from uuid import UUID, uuid4
from datetime import datetime, UTC

if t.TYPE_CHECKING:
    from hexcore.domain.events import DomainEvent


def _sin_eventos() -> t.List["DomainEvent"]:
    """
    El default de `_domain_events`.

    Es una funcion con nombre y no `default_factory=list` porque `DomainEvent` solo existe
    bajo `TYPE_CHECKING` — `events.py` importa este modulo, asi que el import de vuelta seria
    un ciclo— y con `list` pelado el checker infiere `list[Unknown]` y pierde el tipo del
    contenido para todo el que lea el atributo.
    """
    return []


class EventRecorder(BaseModel):
    """
    Lo que sabe acumular eventos de dominio, sin nada mas.

    Es la base comun de `BaseEntity` y de `AggregateRoot`. Existe porque los dos necesitan
    exactamente esta capacidad y **no** deben heredar uno del otro: una entidad clasica es
    estado que se guarda y se lee, y un agregado event-sourced es el fold de su stream. Ver
    el docstring de `AggregateRoot` para por que son hermanas y no madre e hija.

    Compartir la capacidad y no la jerarquia tiene una consecuencia practica: un handler que
    hoy hace `entidad.pull_domain_events()` sigue funcionando contra los dos, sin saber cual
    tiene enfrente.

    **Tiene que heredar `BaseModel`.** Un mixin plano no sirve: pydantic no recolecta los
    atributos privados de bases que no son modelos, y `_domain_events` quedaria como un
    `ModelPrivateAttr` sin resolver — el primer `register_event` falla con
    ``'ModelPrivateAttr' object has no attribute 'append'``.
    """

    # `PrivateAttr(default_factory=list)` y no `= []`. La forma vieja era un default mutable
    # en el cuerpo de la clase; hoy no es un defecto vivo porque pydantic hace
    # `smart_deepcopy` del default al construir cada instancia, pero eso es un detalle
    # interno del que no conviene depender: si dejara de copiarse, dos entidades
    # compartirian la lista y el UoW despacharia los eventos de una junto con los de la otra.
    # El `default_factory` lo garantiza por construccion en vez de por suerte.
    _domain_events: t.List["DomainEvent"] = PrivateAttr(default_factory=_sin_eventos)

    def register_event(self, event: "DomainEvent") -> None:
        """Añade un evento a la lista de la entidad."""
        self._domain_events.append(event)

    def pull_domain_events(self) -> t.List["DomainEvent"]:
        """Entrega los eventos y limpia la lista."""
        events = self._domain_events[:]
        self._domain_events.clear()
        return events

    def clear_domain_events(self) -> None:
        """Limpia la lista de eventos sin entregarlos."""
        self._domain_events.clear()

    @property
    def has_pending_events(self) -> bool:
        """Si queda algo por drenar. Evita que el llamador haga `pull` solo para mirar."""
        return bool(self._domain_events)


class BaseEntity(EventRecorder):
    """
    Clase base para todas las entidades del dominio.

    Proporciona campos comunes y configuración de Pydantic para asegurar consistencia
    y comportamiento predecible en todo el modelo.

    Hereda de `EventRecorder` la capacidad de acumular eventos de dominio
    (`register_event` / `pull_domain_events` / `clear_domain_events`), que el UoW drena
    al comitear.

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

    model_config = ConfigDict(
        from_attributes=True,  # Permite crear modelos desde atributos de objetos (clave para ORMs).
        validate_assignment=True,  # Vuelve a validar la entidad cada vez que se modifica un campo.
        # `frozen=True` hace que las entidades sean inmutables, lo cual es un ideal de DDD.
        # Sin embargo, puede complicar el manejo de estado con un ORM como SQLAlchemy,
        # donde los objetos a menudo se modifican y luego se guardan.
        # Lo cambiamos a False para un enfoque más pragmático.
        frozen=False,
    )

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
