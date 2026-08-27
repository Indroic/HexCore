"""
Repositorio y Unit of Work en memoria, para tests que no necesitan una base.

**Van al kit general y no al de Darwin** porque sirven mucho más allá de identidad: cualquier caso
de uso que hoy se prueba contra SQLite en memoria puede probarse contra esto, y la diferencia son
dos órdenes de magnitud de tiempo. `sqlite_engine` sigue siendo el correcto cuando lo que se
prueba **es** la persistencia —una consulta, un constraint, una migración—; esto es para cuando lo
que se prueba es la lógica que está arriba.

Las tres decisiones que importan:

1. **`FakeRepository` guarda copias, no las entidades que le pasaron.** Sin eso, mutar la entidad
   después de guardarla cambiaría lo guardado, y un test pasaría por una aliasing que en producción
   no existe — el repositorio real serializa a la base. Es el falso positivo más común de un
   repositorio en memoria.
2. **`FakeUnitOfWork` respeta el contrato transaccional**: un `rollback` descarta lo que se escribió
   desde el último `commit`, y un `__aexit__` con excepción rollbackea. Un doble que ignora el
   rollback hace que los tests de "la transacción se deshace" pasen sin probar nada.
3. **`FakeUnitOfWork` no auto-descubre repositorios.** Los recibe explícitos, porque el descubrimiento
   por nombre de clase es justamente el mecanismo que `hexcore.darwin` evita —
   `_repository_key_from_class_name` levanta `ValueError` ante una colisión— y replicarlo en un doble
   traería el mismo problema a los tests.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from hexcore.domain.base import BaseEntity
from hexcore.domain.repositories import IBaseRepository
from hexcore.domain.uow import IUnitOfWork

__all__ = ["FakeRepository", "FakeUnitOfWork"]

T = t.TypeVar("T", bound=BaseEntity)


def _copiar(entidad: T) -> T:
    """
    Una copia de la entidad.

    `model_copy(deep=True)` y no una referencia: ver el punto 1 del docstring del módulo. La copia
    es profunda porque una entidad con una lista o un dict adentro se seguiría compartiendo con una
    copia superficial, y ese es el caso donde el aliasing pasa desapercibido.
    """
    return entidad.model_copy(deep=True)


class FakeRepository(IBaseRepository[T]):
    """
    Repositorio en memoria sobre un `dict` por id.

    Args:
        uow: El Unit of Work. Puede ser un `FakeUnitOfWork` o `None` en los tests que sólo usan el
            repositorio suelto — el `IBaseRepository` real lo guarda y no lo toca en las
            operaciones básicas.
        entities: Entidades iniciales. Se copian al entrar.
        raise_on_missing: Qué lanzar cuando `get_by_id` no encuentra nada. Por defecto
            `KeyError`, para que el test lo vea en vez de recibir `None` y fallar tres líneas
            después.

    Uso::

        from hexcore.testing import FakeRepository, FakeUnitOfWork

        uow = FakeUnitOfWork()
        repo = FakeRepository(uow, entities=[una_entidad])
        uow.add_repository("cosas", repo)
    """

    #: El Unit of Work. `t.Any` y no `IUnitOfWork` —que es lo que declara la base— porque acá
    #: puede ser `None`: los tests que usan el repositorio suelto no tienen uno, y anotarlo con
    #: el tipo de la base hace que el checker marque el `is not None` de `save` como
    #: innecesario. Es la misma concesión que `_model` en la persistencia de Darwin.
    uow: t.Any

    def __init__(
        self,
        uow: t.Any = None,
        *,
        entities: t.Iterable[T] = (),
        raise_on_missing: type[BaseException] = KeyError,
    ) -> None:
        self.uow = uow
        self._por_id: dict[UUID, T] = {e.id: _copiar(e) for e in entities}
        self._raise_on_missing = raise_on_missing

        #: Cada llamada, en orden. Para aseverar que un caso de uso no consultó dos veces lo
        #: mismo, que es el bug de rendimiento que un test contra una base no muestra.
        self.calls: list[tuple[str, t.Any]] = []

    # ── El contrato ───────────────────────────────────────────────────────────
    async def get_by_id(self, entity_id: UUID) -> T:
        self.calls.append(("get_by_id", entity_id))
        try:
            return _copiar(self._por_id[entity_id])
        except KeyError:
            raise self._raise_on_missing(
                f"No hay ninguna entidad con id {entity_id}."
            ) from None

    async def list_all(
        self, limit: int | None = None, offset: int = 0
    ) -> list[T]:
        self.calls.append(("list_all", (limit, offset)))
        # El orden de inserción, que es determinista en un dict de Python 3.7+. Un `set` haría
        # que el orden dependiera del hash y los tests de paginación fallarían una vez cada
        # tanto.
        todas = [_copiar(e) for e in self._por_id.values()]
        recortadas = todas[offset:]
        return recortadas if limit is None else recortadas[:limit]

    async def save(self, entity: T) -> T:
        self.calls.append(("save", entity.id))
        self._por_id[entity.id] = _copiar(entity)
        if self.uow is not None and hasattr(self.uow, "collect_entity"):
            # El repositorio real registra la entidad en el UoW para que sus eventos se
            # despachen al commit. Omitirlo acá haría que un test de eventos de dominio pase
            # con el fake y falle con el repositorio de verdad.
            self.uow.collect_entity(entity)
        return _copiar(entity)

    async def delete(self, entity: T) -> None:
        self.calls.append(("delete", entity.id))
        self._por_id.pop(entity.id, None)

    # ── Extras para el test ───────────────────────────────────────────────────
    @property
    def stored(self) -> list[T]:
        """Lo guardado, en orden de inserción. Copias, igual que todo lo que sale de acá."""
        return [_copiar(e) for e in self._por_id.values()]

    def __len__(self) -> int:
        return len(self._por_id)

    def __contains__(self, entity_id: object) -> bool:
        return entity_id in self._por_id

    def seed(self, *entities: T) -> "FakeRepository[T]":
        """Agrega entidades sin pasar por `save`. Devuelve `self` para poder encadenar."""
        for entidad in entities:
            self._por_id[entidad.id] = _copiar(entidad)
        return self

    def snapshot(self) -> dict[UUID, T]:
        """Una copia del estado, para poder restaurarlo. Lo usa `FakeUnitOfWork`."""
        return {k: _copiar(v) for k, v in self._por_id.items()}

    def restore(self, snapshot: t.Mapping[UUID, T]) -> None:
        """Vuelve al estado de un `snapshot`. Es el rollback."""
        self._por_id = {k: _copiar(v) for k, v in snapshot.items()}

    def count_calls(self, method: str) -> int:
        """Cuántas veces se llamó a un método. Para los asserts de "no consultes dos veces"."""
        return sum(1 for nombre, _ in self.calls if nombre == method)


class FakeUnitOfWork(IUnitOfWork):
    """
    Unit of Work en memoria, con transaccionalidad real sobre los `FakeRepository` que le pases.

    Args:
        repositories: `{clave: repositorio}`. Explícito y no autodescubierto — ver el punto 3 del
            docstring del módulo.
        event_bus: Un bus para `dispatch_events`, o `None` para juntar los eventos y no publicarlos.

    Uso::

        from hexcore.testing import FakeRepository, FakeUnitOfWork

        uow = FakeUnitOfWork()
        uow.add_repository("cosas", FakeRepository())

        async with uow:
            await uow.repositories["cosas"].save(entidad)
            await uow.commit()
    """

    def __init__(
        self,
        repositories: t.Mapping[str, t.Any] | None = None,
        *,
        event_bus: t.Any = None,
    ) -> None:
        super().__init__()
        self.repositories = dict(repositories or {})
        self.event_bus = event_bus

        #: Contadores, para aseverar que un caso de uso commiteó **una** vez.
        self.commits = 0
        self.rollbacks = 0

        #: Los eventos despachados, en orden. Si no hay `event_bus`, quedan sólo acá.
        self.dispatched: list[t.Any] = []

        self._trackeadas: list[BaseEntity] = []
        self._punto_de_guardado: dict[str, dict[UUID, t.Any]] = {}
        self._tomar_punto_de_guardado()

    def add_repository(self, key: str, repository: t.Any) -> "FakeUnitOfWork":
        """
        Registra un repositorio y toma un punto de guardado nuevo.

        Devuelve `self` para poder encadenar. El punto de guardado se re-toma porque un
        repositorio agregado después del `__init__` traería su propio estado inicial, y sin esto
        un `rollback` lo borraría.
        """
        self.repositories[key] = repository
        self._tomar_punto_de_guardado()
        return self

    # ── El contrato transaccional ─────────────────────────────────────────────
    async def commit(self) -> None:
        """
        Confirma: fija el punto de guardado y despacha los eventos.

        Los eventos se despachan **después** de fijar el punto, igual que en el UoW real: un
        handler que falla no tiene que deshacer lo que ya se confirmó.
        """
        self.commits += 1
        self._tomar_punto_de_guardado()
        await self.dispatch_events()

    async def rollback(self) -> None:
        """
        Deshace: restaura cada repositorio a su punto de guardado y descarta los eventos.

        Restaurar de verdad —y no sólo contar la llamada— es lo que hace que un test de "la
        transacción se deshace" pruebe algo.
        """
        self.rollbacks += 1
        for clave, repositorio in self.repositories.items():
            restaurar = getattr(repositorio, "restore", None)
            if restaurar is not None:
                restaurar(self._punto_de_guardado.get(clave, {}))
        self._trackeadas.clear()

    def collect_entity(self, entity: BaseEntity) -> None:
        self._trackeadas.append(entity)

    def collect_domain_events(self) -> list[t.Any]:
        """
        Los eventos de las entidades trackeadas.

        Se leen con `getattr` porque `BaseEntity` no obliga a tener eventos: una entidad que no
        los emite no debería tener que declarar una lista vacía.
        """
        eventos: list[t.Any] = []
        for entidad in self._trackeadas:
            eventos.extend(getattr(entidad, "_events", ()) or ())
        return eventos

    async def dispatch_events(self) -> None:
        eventos = self.collect_domain_events()
        self.dispatched.extend(eventos)
        if self.event_bus is not None:
            for evento in eventos:
                await self.event_bus.publish(evento)
        self.clear_tracked_entities()

    def clear_tracked_entities(self) -> None:
        self._trackeadas.clear()

    # ── Interno ───────────────────────────────────────────────────────────────
    def _tomar_punto_de_guardado(self) -> None:
        self._punto_de_guardado = {
            clave: repositorio.snapshot()
            for clave, repositorio in self.repositories.items()
            if hasattr(repositorio, "snapshot")
        }
