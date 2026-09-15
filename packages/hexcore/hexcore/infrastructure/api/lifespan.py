"""
Lifespan composable a partir de steps.

El `lifespan` de una app real orquesta a mano: engine SQL → documentos Beanie →
publisher realtime → inyección del event bus en config → warmup de cachés →
consumidores → pool de la cola. El orden importa y no está documentado en ningún sitio,
y cada paso opcional acaba envuelto en su propio `try/except`.

Requisitos que cumple esta implementación, y que son la parte que se hace mal a mano:

- **Teardown en orden inverso.**
- **Teardown garantizado de los steps que sí arrancaron**, aunque uno posterior falle.
- **`on_error="warn"` por step**: un warmup de caché no debe tumbar el arranque.
- Un log por step con su duración.
"""
from __future__ import annotations

import logging
import time
import typing as t
from contextlib import asynccontextmanager

logger = logging.getLogger("hexcore.api.lifespan")

__all__ = [
    "StartupStep",
    "ErrorPolicy",
    "build_lifespan",
    "CallableStep",
    "SqlEngineStep",
    "BeanieStep",
    "EventBusStep",
    "CacheStep",
    "ProcrastinateStep",
    "CronSeedStep",
]

ErrorPolicy = t.Literal["raise", "warn"]


@t.runtime_checkable
class StartupStep(t.Protocol):
    """Un paso del arranque. `stop()` es opcional."""

    name: str

    async def start(self) -> None: ...


def build_lifespan(
    *steps: StartupStep,
    on_error: ErrorPolicy = "raise",
) -> t.Callable[[t.Any], t.AsyncContextManager[None]]:
    """
    Compone un lifespan de FastAPI a partir de steps.

    Args:
        *steps: Los pasos, en orden de arranque. El teardown va en orden inverso.
        on_error: Política por defecto. Cada step puede sobreescribirla con un atributo
            `on_error` propio, que es lo que permite decir "este warmup puede fallar" sin
            cambiar la política global.

    Returns:
        Un context manager apto para ``FastAPI(lifespan=...)``.

    Uso::

        app = create_app(lifespan=build_lifespan(
            SqlEngineStep(),
            BeanieStep(documents=MONGO_DOCUMENTS),
            EventBusStep(RealtimeEventDispatcher()),
            ProcrastinateStep(procrastinate_app),
            CallableStep("warm-caches", warm_validation_cache, on_error="warn"),
        ))
    """
    @asynccontextmanager
    async def lifespan(app: t.Any) -> t.AsyncIterator[None]:
        del app  # el contrato de FastAPI lo pasa; los steps no lo necesitan
        started: list[StartupStep] = []
        try:
            for step in steps:
                await _start_step(step, on_error, started)
            yield
        finally:
            # Inverso y sobre `started`, no sobre `steps`: parar algo que nunca arrancó
            # es cómo un fallo de arranque se convierte en dos errores en el log.
            await _stop_steps(started)

    return lifespan


async def _start_step(
    step: StartupStep,
    default_policy: ErrorPolicy,
    started: list[StartupStep],
) -> None:
    policy: ErrorPolicy = getattr(step, "on_error", None) or default_policy
    start_time = time.perf_counter()
    try:
        await step.start()
    except Exception as exc:
        elapsed = (time.perf_counter() - start_time) * 1000
        if policy == "warn":
            logger.warning(
                "[lifespan] step '%s' falló en %.1fms (%s: %s); se continúa porque "
                "on_error='warn'.",
                step.name,
                elapsed,
                type(exc).__name__,
                exc,
            )
            return
        logger.error(
            "[lifespan] step '%s' falló en %.1fms (%s: %s); se aborta el arranque.",
            step.name,
            elapsed,
            type(exc).__name__,
            exc,
        )
        raise

    elapsed = (time.perf_counter() - start_time) * 1000
    logger.info("[lifespan] step '%s' arrancó en %.1fms", step.name, elapsed)
    started.append(step)


async def _stop_steps(started: list[StartupStep]) -> None:
    for step in reversed(started):
        stop = getattr(step, "stop", None)
        if stop is None:
            continue
        start_time = time.perf_counter()
        try:
            await stop()
        except Exception:
            # Un teardown que falla no debe impedir los siguientes ni tapar la excepción
            # que provocó el apagado.
            logger.exception("[lifespan] step '%s' falló al parar", step.name)
            continue
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.info("[lifespan] step '%s' paró en %.1fms", step.name, elapsed)


# ── Steps de serie ────────────────────────────────────────────────────────────


class CallableStep:
    """
    Envuelve un par de corutinas sueltas como step.

    Reemplaza el `try/except` a mano alrededor de cada paso opcional del arranque.
    """

    def __init__(
        self,
        name: str,
        start: t.Callable[[], t.Awaitable[None]],
        stop: t.Callable[[], t.Awaitable[None]] | None = None,
        *,
        on_error: ErrorPolicy | None = None,
    ) -> None:
        self.name = name
        self.on_error = on_error
        self._start = start
        self._stop = stop

    async def start(self) -> None:
        await self._start()

    async def stop(self) -> None:
        if self._stop is not None:
            await self._stop()


class SqlEngineStep:
    """
    Inicializa el engine de SQLAlchemy y lo cierra al apagar.

    Envuelve `init_engine()` / `dispose_engine()` de F2, así que hereda
    `expire_on_commit=False`, la normalización del DSN y el tuning del pool.
    """

    name = "sql-engine"

    def __init__(
        self,
        url: str | None = None,
        *,
        pool: t.Any = None,
        on_error: ErrorPolicy | None = None,
        **engine_kwargs: t.Any,
    ) -> None:
        self.on_error = on_error
        self._url = url
        self._pool = pool
        self._engine_kwargs = engine_kwargs

    async def start(self) -> None:
        from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
            init_engine,
        )

        init_engine(url=self._url, pool=self._pool, **self._engine_kwargs)

    async def stop(self) -> None:
        from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
            dispose_engine,
        )

        await dispose_engine()


class BeanieStep:
    """Inicializa los documentos de Beanie/MongoDB."""

    name = "beanie"

    def __init__(
        self,
        documents: t.Sequence[t.Any] | None = None,
        *,
        on_error: ErrorPolicy | None = None,
    ) -> None:
        self.on_error = on_error
        self._documents = documents

    async def start(self) -> None:
        from hexcore.infrastructure.repositories.orms.beanie.utils import (
            init_beanie_documents,
        )

        if self._documents is None:
            await init_beanie_documents()
        else:
            await init_beanie_documents(list(self._documents))


class EventBusStep:
    """
    Inyecta un event bus en `ServerConfig`.

    Es el paso que la app real hacía a mano (`config.event_bus = ...`) y que hay que
    hacer *después* de que el bus tenga sus dependencias listas.
    """

    name = "event-bus"

    def __init__(self, bus: t.Any, *, on_error: ErrorPolicy | None = None) -> None:
        self.on_error = on_error
        self._bus = bus
        self._previous: t.Any = None

    async def start(self) -> None:
        from hexcore.config import LazyConfig

        config = LazyConfig.get_config()
        self._previous = config.event_bus
        config.event_bus = self._bus

    async def stop(self) -> None:
        from hexcore.config import LazyConfig

        # Se restaura para que un lifespan en tests no contamine el siguiente.
        LazyConfig.get_config().event_bus = self._previous


class CacheStep:
    """Inyecta un backend de cache en `ServerConfig` y lo cierra si sabe cómo."""

    name = "cache"

    def __init__(self, backend: t.Any, *, on_error: ErrorPolicy | None = None) -> None:
        self.on_error = on_error
        self._backend = backend
        self._previous: t.Any = None

    async def start(self) -> None:
        from hexcore.config import LazyConfig

        config = LazyConfig.get_config()
        self._previous = config.cache_backend
        config.cache_backend = self._backend

    async def stop(self) -> None:
        from hexcore.config import LazyConfig

        LazyConfig.get_config().cache_backend = self._previous
        close = getattr(self._backend, "aclose", None) or getattr(
            self._backend, "close", None
        )
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result


class ProcrastinateStep:
    """Abre y cierra el pool de una app de Procrastinate."""

    name = "procrastinate"

    def __init__(self, app: t.Any, *, on_error: ErrorPolicy | None = None) -> None:
        self.on_error = on_error
        self._app = app

    async def start(self) -> None:
        await self._app.open_async()

    async def stop(self) -> None:
        await self._app.close_async()


class CronSeedStep:
    """
    Siembra las definiciones de cron. Idempotente (ver F7).

    Va después de `SqlEngineStep`, obviamente: necesita el engine.
    """

    name = "cron-seed"

    def __init__(
        self,
        jobs: t.Sequence[t.Any],
        *,
        create_tables: bool = False,
        on_error: ErrorPolicy | None = None,
    ) -> None:
        self.on_error = on_error
        self._jobs = jobs
        self._create_tables = create_tables

    async def start(self) -> None:
        from hexcore.infrastructure.cqrs.cron_sql import create_cron_tables, seed_cron_jobs

        if self._create_tables:
            await create_cron_tables()
        inserted = await seed_cron_jobs(list(self._jobs))
        logger.info("[lifespan] cron-seed insertó %d definiciones", inserted)
