"""
Dobles de prueba para los puertos de HexCore.

Sin dependencias opcionales: sólo stdlib y los puertos de dominio.
"""
from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

from hexcore.domain.cqrs.cron import ILockProvider
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

__all__ = ["RecordedEnqueue", "InMemoryTaskEnqueuer", "FakeLockProvider"]


@dataclass(frozen=True)
class RecordedEnqueue:
    """Un encolado registrado."""

    kind: t.Literal["command", "event", "handler", "task"]
    name: str
    payload: dict[str, t.Any] = field(default_factory=dict)
    queue: str = "default"


class InMemoryTaskEnqueuer(ITaskEnqueuer):
    """
    Registra lo encolado en vez de mandarlo a un broker.

    Sirve para asertar sobre el Smart Routing sin levantar Procrastinate ni Celery.

    Uso::

        enqueuer = InMemoryTaskEnqueuer()
        bus = InMemoryCommandBus(registry=registry, enqueuer=enqueuer, serializer=ser)

        await bus.dispatch(SendEmailCommand(to="a@b.c"))

        assert enqueuer.command_names == ["SendEmailCommand"]
        assert enqueuer.commands[0].queue == "emails"
    """

    def __init__(self, *, fail_on: t.Container[str] = ()) -> None:
        """
        Args:
            fail_on: Nombres para los que `enqueue_*` debe lanzar, para probar el camino
                de error sin tener que tirar un broker.
        """
        self.recorded: list[RecordedEnqueue] = []
        self._fail_on = fail_on

    # ── Puerto ────────────────────────────────────────────────────────────────

    async def enqueue_command(
        self, command_name: str, payload: dict[str, t.Any], queue: str
    ) -> None:
        self._record("command", command_name, payload, queue)

    async def enqueue_event(
        self, event_name: str, payload: dict[str, t.Any], queue: str
    ) -> None:
        self._record("event", event_name, payload, queue)

    async def enqueue_handler(
        self, handler_name: str, payload: dict[str, t.Any], queue: str
    ) -> None:
        self._record("handler", handler_name, payload, queue)

    async def enqueue_task(
        self, task_name: str, payload: dict[str, t.Any], queue: str
    ) -> None:
        self._record("task", task_name, payload, queue)

    def _record(
        self,
        kind: t.Literal["command", "event", "handler", "task"],
        name: str,
        payload: dict[str, t.Any],
        queue: str,
    ) -> None:
        if name in self._fail_on:
            raise RuntimeError(f"InMemoryTaskEnqueuer configurado para fallar en {name!r}")
        self.recorded.append(RecordedEnqueue(kind, name, payload, queue))

    # ── Aserciones ────────────────────────────────────────────────────────────

    @property
    def commands(self) -> list[RecordedEnqueue]:
        return self._of_kind("command")

    @property
    def events(self) -> list[RecordedEnqueue]:
        return self._of_kind("event")

    @property
    def handlers(self) -> list[RecordedEnqueue]:
        return self._of_kind("handler")

    @property
    def tasks(self) -> list[RecordedEnqueue]:
        return self._of_kind("task")

    @property
    def command_names(self) -> list[str]:
        return [item.name for item in self.commands]

    @property
    def task_names(self) -> list[str]:
        return [item.name for item in self.tasks]

    @property
    def handler_names(self) -> list[str]:
        return [item.name for item in self.handlers]

    def _of_kind(self, kind: str) -> list[RecordedEnqueue]:
        return [item for item in self.recorded if item.kind == kind]

    def clear(self) -> None:
        self.recorded.clear()

    def __len__(self) -> int:
        return len(self.recorded)

    def __bool__(self) -> bool:
        # Sin esto, `__len__` hace que un enqueuer vacío sea falsy, y cualquier código
        # que compruebe `if enqueuer:` lo descartaría justo cuando aún no ha encolado
        # nada — que es siempre, al empezar un test.
        return True

    def __repr__(self) -> str:
        return f"InMemoryTaskEnqueuer({len(self.recorded)} encolados)"


class FakeLockProvider(ILockProvider):
    """
    Lock provider determinista.

    Tres modos, para los tres escenarios que hay que probar en un scheduler:

    - `FakeLockProvider()` — concede siempre. "Soy la única réplica."
    - `FakeLockProvider(grant=False)` — niega siempre. "Otra réplica va por delante."
    - `FakeLockProvider(shared=True)` — se comporta como un lock real en memoria: el
      primero que pide una clave la obtiene y el resto no. Para probar dos schedulers a
      la vez en el mismo proceso.
    """

    def __init__(
        self,
        *,
        grant: bool = True,
        shared: bool = False,
        raise_on_acquire: BaseException | None = None,
    ) -> None:
        self.grant = grant
        self.shared = shared
        self.raise_on_acquire = raise_on_acquire
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.held: set[str] = set()

    async def acquire_lock(self, lock_key: str, ttl_seconds: int) -> bool:
        self.acquired.append(lock_key)
        if self.raise_on_acquire is not None:
            raise self.raise_on_acquire
        if self.shared:
            if lock_key in self.held:
                return False
            self.held.add(lock_key)
            return True
        return self.grant

    async def release_lock(self, lock_key: str) -> None:
        self.released.append(lock_key)
        self.held.discard(lock_key)
