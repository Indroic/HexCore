"""
`AggregateRoot`: un agregado cuyo estado es el fold de su stream de eventos.

La diferencia con una entidad clásica no es de estilo. Una `BaseEntity` **es** estado: se
guarda, se lee y se modifica. Un `AggregateRoot` **deriva** su estado: lo único que se
persiste son los hechos, y el objeto en memoria es el resultado de aplicarlos en orden. De
ahí sale todo lo demás — que se pueda reconstruir el pasado, que una proyección nueva pueda
llenarse con años de historia, y que la concurrencia se resuelva comparando una versión en
vez de bloqueando una fila.
"""
from __future__ import annotations

import re
import typing as t
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, PrivateAttr

from hexcore.domain.base import EventRecorder
from hexcore.domain.cqrs.dispatch import matching_types
from hexcore.domain.events import DomainEvent

from .exceptions import UnhandledEventError
from .snapshots import Snapshot

__all__ = ["AggregateRoot", "when"]

F = t.TypeVar("F", bound=t.Callable[..., None])

#: Atributo que `@when` deja en el método. Lo lee `__init_subclass__`.
_MARCA = "__when_events__"

_CAMEL_A_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


def when(*event_types: type[DomainEvent]) -> t.Callable[[F], F]:
    """
    Marca un método como el mutador de esos tipos de evento.

    ::

        class Pedido(AggregateRoot):
            total: int

            @when(PedidoCreado)
            def _crear(self, evento: PedidoCreado) -> None:
                self.id = evento.pedido_id
                self.total = 0

    El mutador **sólo muta**. No valida reglas de negocio, no lanza y no emite eventos: lo
    que llega ahí ya pasó, y durante un replay se está reconstruyendo historia, no
    decidiendo. Las reglas van en el método de negocio que llama a `raise_event()`.

    Se eligió registro por decorador y no las dos alternativas obvias. `singledispatchmethod`
    resuelve las anotaciones en runtime y pyright strict lo soporta mal. La convención por
    nombre (`_when_PedidoCreado`) se rompe en silencio con un rename: la herramienta cambia
    la clase del evento, el método se queda con el nombre viejo, y el agregado deja de
    aplicar ese evento sin que nada avise. Un decorador referencia la clase de verdad, así
    que un rename lo arrastra.
    """
    if not event_types:
        raise ValueError("@when necesita al menos un tipo de evento.")

    for tipo in event_types:
        # Redundante para el checker —la firma ya dice `type[DomainEvent]`— y se queda
        # igual, por el mismo motivo que el `isinstance` de `resolve_dotted`: la anotación
        # describe el contrato y esta línea lo defiende cuando el llamador no lo cumple.
        # `@when` lo escribe el consumidor, y un `@when(PedidoPagado())` con la instancia en
        # vez de la clase —o el nombre del evento como string— es el error de tipeo natural.
        # Sin la guarda, el tipo entra al registro, nunca coincide con ningún evento, y el
        # mutador no se invoca jamás sin que nada avise.
        if not (
            isinstance(tipo, type)  # pyright: ignore[reportUnnecessaryIsInstance]
            and issubclass(tipo, DomainEvent)  # pyright: ignore[reportUnnecessaryIsInstance]
        ):
            raise TypeError(
                f"@when sólo acepta subclases de DomainEvent; recibió {tipo!r}."
            )

    def decorador(metodo: F) -> F:
        previos: tuple[type[DomainEvent], ...] = getattr(metodo, _MARCA, ())
        setattr(metodo, _MARCA, previos + event_types)
        return metodo

    return decorador


class AggregateRoot(EventRecorder):
    """
    Raíz de agregado event-sourced.

    **Hermana de `BaseEntity`, no hija.** Las dos heredan de `EventRecorder`, así que un
    handler que hace `pull_domain_events()` funciona contra ambas, pero ninguna hereda de la
    otra. Cuatro razones, en orden de peso:

    1. **La trampa del Unit of Work.** `SqlAlchemyUnitOfWork.collect_domain_entities()`
       recorre la sesión, reconoce las entidades de dominio con un `isinstance` y les drena
       los eventos. Si un agregado event-sourced fuera además una `BaseEntity` pegada a un
       modelo del ORM, sus eventos se despacharían **dos veces**: una por el UoW y otra por
       `EventSourcedRepository`. Que sean tipos distintos vuelve ese error imposible de
       escribir por accidente.
    2. **`validate_assignment`.** `BaseEntity` revalida el modelo entero en cada asignación,
       que es lo correcto cuando el estado llega de afuera. Acá el estado se construye
       aplicando eventos que ya se validaron al crearse, y un replay de cinco mil eventos con
       tres asignaciones cada uno serían quince mil validaciones completas para llegar a un
       resultado que ya era válido.
    3. **Los campos de `BaseEntity` sobran o mienten.** `is_active` es un borrado lógico —
       en un agregado eso es un evento, `PedidoCancelado`, no una bandera— y `updated_at` lo
       pisa el ORM con su `onupdate`. Un agregado cuyo estado es el fold de su stream no
       puede tener campos que escriba otro mecanismo.
    4. **Lo que sí necesita, `BaseEntity` no lo tiene**: `version`, `stream_id`, y la
       distinción entre eventos ya confirmados y eventos pendientes de escritura.

    Los eventos pendientes se guardan en `_domain_events`, el mismo de `EventRecorder`. La
    salida normal es `mark_events_as_committed()`, que además avanza la versión;
    `pull_domain_events()` los drena **sin** avanzarla, así que usarlo sobre un agregado
    desincroniza `version` del stream. Sirve para inspeccionar en un test, no para el camino
    de escritura.
    """

    model_config = ConfigDict(
        from_attributes=True,
        # Ver la razón 2 del docstring de la clase: el estado se construye aplicando eventos
        # ya validados, no se valida el resultado en cada paso.
        validate_assignment=False,
    )

    #: La categoría del stream. Por defecto, el nombre de la clase en snake_case.
    #:
    #: Fijalo explícitamente en cuanto haya un stream en producción: es parte del
    #: `stream_id`, así que renombrar la clase con el default puesto deja huérfanos todos los
    #: streams escritos con el nombre anterior.
    __stream_type__: t.ClassVar[str] = ""

    #: `{tipo_de_evento: nombre_del_método}`. Lo arma `__init_subclass__`.
    __mutators__: t.ClassVar[dict[type[DomainEvent], str]] = {}

    #: Si un evento sin mutador es un error. Ver `UnhandledEventError`.
    __strict_mutators__: t.ClassVar[bool] = True

    id: UUID = Field(default_factory=uuid4)

    #: La versión **confirmada en el almacén**, no la actual en memoria.
    #:
    #: Sólo avanza en `mark_events_as_committed()`, o sea después de que el `append` haya
    #: salido bien. Es lo que hace que un `ConcurrencyError` deje el agregado intacto y
    #: reintentable: los eventos pendientes siguen ahí y la versión sigue siendo la que el
    #: almacén rechazó, así que recargar y reaplicar es posible.
    _version: int = PrivateAttr(default=0)

    def __init_subclass__(cls, **kwargs: t.Any) -> None:
        super().__init_subclass__(**kwargs)

        if not cls.__dict__.get("__stream_type__"):
            cls.__stream_type__ = _CAMEL_A_SNAKE.sub("_", cls.__name__).lower()

        # Se recorre el MRO al revés (de la base a la clase) para que una subclase pueda
        # redefinir el mutador de un evento que su padre ya manejaba: lo que se escribe
        # después gana. Lo que no se permite es la ambigüedad dentro de un mismo nivel.
        mutadores: dict[type[DomainEvent], str] = {}
        for clase in reversed(cls.__mro__):
            reclamados_aqui: dict[type[DomainEvent], str] = {}
            for nombre, atributo in vars(clase).items():
                for tipo in getattr(atributo, _MARCA, ()):
                    anterior = reclamados_aqui.get(tipo)
                    if anterior is not None:
                        raise TypeError(
                            f"{clase.__name__} declara dos mutadores para "
                            f"{tipo.__name__}: '{anterior}' y '{nombre}'. No se elige uno "
                            f"por precedencia porque no hay ninguna razón para preferir a "
                            f"cualquiera de los dos: es una ambigüedad, y el evento se "
                            f"aplicaría de una forma distinta según el orden de definición."
                        )
                    reclamados_aqui[tipo] = nombre
            mutadores.update(reclamados_aqui)

        cls.__mutators__ = mutadores

    # ── Lectura ───────────────────────────────────────────────────────────────
    @property
    def version(self) -> int:
        """La versión confirmada en el almacén. 0 si el agregado todavía no se guardó."""
        return self._version

    @property
    def uncommitted_events(self) -> tuple[DomainEvent, ...]:
        """Los eventos emitidos y todavía no persistidos, en orden."""
        return tuple(self._domain_events)

    @property
    def stream_id(self) -> str:
        """El stream de esta instancia."""
        return self.stream_id_for(self.id)

    @classmethod
    def stream_id_for(cls, aggregate_id: UUID | str) -> str:
        """
        El `stream_id` de un agregado de este tipo, sin tener que instanciarlo.

        Lo necesita el repositorio para leer antes de que exista el objeto.
        """
        return f"{cls.__stream_type__}-{aggregate_id}"

    # ── Escritura ─────────────────────────────────────────────────────────────
    def raise_event(self, event: DomainEvent) -> None:
        """
        Lo que llaman los métodos de negocio: aplica el evento **y** lo encola.

        El orden importa. Se aplica primero, así que si el mutador falla el evento no queda
        encolado: un evento pendiente que el propio agregado no supo aplicar se escribiría en
        el almacén y rompería todos los replays futuros.
        """
        self.apply(event)
        self.register_event(event)

    def apply(self, event: DomainEvent) -> None:
        """
        Muta el estado según el evento. **No lo encola.**

        La usan `raise_event()` y el replay, y es esa doble vida lo que garantiza que
        reconstruir un agregado dé exactamente el mismo estado que construirlo en vivo: es
        literalmente el mismo código.

        El mutador se busca recorriendo la jerarquía del evento, de lo más específico a lo
        más general, con la misma resolución que usan los buses. Un `@when(PedidoEvent)`
        cubre a toda la familia, y un `@when(PedidoPagado)` le gana por ser más preciso.

        Raises:
            UnhandledEventError: Si no hay mutador y `__strict_mutators__` está activo.
        """
        for tipo in matching_types(type(event)):
            nombre = self.__mutators__.get(tipo)
            if nombre is not None:
                getattr(self, nombre)(event)
                return

        if self.__strict_mutators__:
            raise UnhandledEventError(type(self).__name__, type(event).__name__)

    def mark_events_as_committed(self) -> None:
        """
        Confirma que los pendientes se escribieron: avanza la versión y vacía la cola.

        La llama `EventSourcedRepository.save()` **sólo si el `append` salió bien**.
        """
        self._version += len(self._domain_events)
        self.clear_domain_events()

    # ── Snapshots ─────────────────────────────────────────────────────────────
    def snapshot_state(self) -> dict[str, t.Any]:
        """
        El estado serializable para un snapshot.

        `mode="json"` para que lo que se guarde sea del mismo tipo que va a volver: un `UUID`
        o un `datetime` que se guardan como objetos de Python y se releen desde JSON no son
        el mismo valor, y la diferencia aparece recién al comparar.
        """
        return self.model_dump(mode="json")

    @classmethod
    def restore_snapshot(cls, snapshot: Snapshot) -> t.Self:
        """
        Reconstruye el agregado desde un snapshot, en la versión que este refleja.

        Usa `model_validate` y no `model_construct`: un snapshot es datos que estuvieron
        guardados, y pudieron quedar mal por un bug de `snapshot_state()` o por un campo que
        cambió de forma entre versiones. Validarlo hace que el problema salte acá, con el
        `stream_id` a mano, en vez de más tarde y en otro lugar. Es una vez por carga, no una
        por evento, así que el costo no está en el camino caliente.
        """
        agregado = cls.model_validate(snapshot.state)
        agregado._version = snapshot.version
        return agregado

    # ── Reconstrucción ────────────────────────────────────────────────────────
    @classmethod
    def load_from_history(
        cls,
        events: t.Sequence[DomainEvent],
        *,
        snapshot: Snapshot | None = None,
        aggregate_id: UUID | None = None,
    ) -> t.Self:
        """
        Reconstruye el agregado aplicando su historial.

        Args:
            events: Los eventos en orden. Si hay snapshot, **sólo los posteriores**.
            snapshot: El punto de partida. Sin él se arranca del vacío.
            aggregate_id: El id que tiene que tener el agregado reconstruido.

        `aggregate_id` no es opcional en la práctica, aunque lo sea en la firma: sin él, el
        `default_factory=uuid4` del campo `id` le pone uno **nuevo y distinto en cada carga**,
        así que `stream_id` apuntaría a un stream que no es el que se acaba de leer y el
        `save()` siguiente escribiría en el lugar equivocado. El repositorio siempre lo pasa,
        porque siempre sabe qué id pidió.

        Se deja opcional para el caso en que el propio historial lo asigna — un
        `PedidoCreado` que lleva el `pedido_id` y cuyo mutador hace `self.id = ...` —, que es
        el estilo más fiel al event sourcing y el único que funciona si el agregado se
        reconstruye desde un stream leído por categoría, sin un id a mano.

        Sin snapshot se parte de `model_construct()`, que crea la instancia sin validar y
        **deja ausentes los campos requeridos** hasta que el primer evento los asigna. Eso es
        lo que permite que un agregado declare `total: Decimal` sin default: el modelo no
        admite estados inválidos, y el vacío previo al replay no llega a ser uno porque nadie
        puede observarlo — el único código que corre entre la construcción y el primer evento
        es este bucle.

        La alternativa sería exigir que todo agregado tenga un constructor vacío válido, o
        sea llenar el modelo de defaults que no significan nada. Eso sí produce estados
        inválidos observables.
        """
        agregado = cls.model_construct() if snapshot is None else cls.restore_snapshot(snapshot)

        # Antes de aplicar, para que un mutador que lea `self.id` vea el correcto y para que
        # un evento del historial que lo asigne pueda ganarle a este valor.
        if aggregate_id is not None:
            agregado.id = aggregate_id

        for evento in events:
            agregado.apply(evento)

        agregado._version = (snapshot.version if snapshot is not None else 0) + len(events)
        # El replay reconstruye lo que ya está en el almacén: nada de esto está pendiente de
        # escritura. `apply()` no encola, pero `restore_snapshot()` pasa por `model_validate`
        # y un validador del consumidor podría emitir algo; se limpia para no reescribir
        # historia ya escrita.
        agregado.clear_domain_events()
        return agregado
