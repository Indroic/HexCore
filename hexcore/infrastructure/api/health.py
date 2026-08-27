"""
Health checks: liveness sin I/O, readiness que sondea de verdad.

Un `/health/detailed` que devuelve `{"status": "ok"}` hardcodeado es un health check que
no puede fallar, y eso es **peor** que no tenerlo: el orquestador lo lee como "sano" con
la base de datos caída y sigue mandándole tráfico.

La librería sabe qué dependencias hay configuradas, así que puede sondearlas. Cada sonda
tiene su propio timeout: un health check que se cuelga es un health check que tumba el
deploy.
"""
from __future__ import annotations

import asyncio
import logging
import time
import typing as t

from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

logger = logging.getLogger("hexcore.api.health")

__all__ = [
    "HealthStatus",
    "DependencyReport",
    "HealthReport",
    "Probe",
    "ResponseFactory",
    "check_health",
    "register_health_routes",
    "default_probes",
]

HealthStatus = t.Literal["ok", "degraded", "down"]


class DependencyReport(BaseModel):
    """Resultado de sondear una dependencia."""

    name: str
    status: HealthStatus
    latency_ms: float
    detail: str | None = None


class HealthReport(BaseModel):
    """Resultado global. `status` es el peor de las dependencias."""

    status: HealthStatus
    deep: bool = False
    dependencies: list[DependencyReport] = Field(default_factory=list)

    @property
    def http_status(self) -> int:
        """503 si algo está caído; 200 en el resto de los casos."""
        return 503 if self.status == "down" else 200


#: Adapta el cuerpo de las rutas de health a la forma que ya publica una app. Recibe el
#: informe y devuelve lo que haya que serializar.
ResponseFactory = t.Callable[[HealthReport], t.Any]


class Probe(t.NamedTuple):
    """Una sonda con nombre y timeout propio."""

    name: str
    check: t.Callable[[], t.Awaitable[None]]
    timeout: float = 2.0
    # Una dependencia no crítica reporta "degraded" en vez de "down": un cache caído
    # ralentiza, no impide servir.
    critical: bool = True


async def check_health(
    *,
    deep: bool = False,
    probes: t.Sequence[Probe] | None = None,
) -> HealthReport:
    """
    Evalúa la salud del proceso.

    Args:
        deep: ``False`` → liveness: responde siempre 200 sin tocar nada, que es lo que
            un liveness probe debe hacer (si sondea dependencias, un Redis caído provoca
            que Kubernetes reinicie una app perfectamente sana). ``True`` → readiness:
            sondea las dependencias configuradas.
        probes: Sondas a ejecutar. Por defecto, las que `default_probes()` deduzca de la
            configuración.

    Returns:
        El informe, con el estado y la latencia por dependencia.
    """
    if not deep:
        return HealthReport(status="ok", deep=False)

    selected = list(probes) if probes is not None else default_probes()
    if not selected:
        return HealthReport(status="ok", deep=True)

    reports = await asyncio.gather(*(_run_probe(probe) for probe in selected))

    if any(report.status == "down" for report in reports):
        overall: HealthStatus = "down"
    elif any(report.status == "degraded" for report in reports):
        overall = "degraded"
    else:
        overall = "ok"

    return HealthReport(status=overall, deep=True, dependencies=list(reports))


async def _run_probe(probe: Probe) -> DependencyReport:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(probe.check(), timeout=probe.timeout)
    except asyncio.TimeoutError:
        return DependencyReport(
            name=probe.name,
            status="down" if probe.critical else "degraded",
            latency_ms=_elapsed_ms(start),
            detail=f"timeout tras {probe.timeout}s",
        )
    except Exception as exc:
        return DependencyReport(
            name=probe.name,
            status="down" if probe.critical else "degraded",
            latency_ms=_elapsed_ms(start),
            detail=f"{type(exc).__name__}: {exc}",
        )

    return DependencyReport(
        name=probe.name, status="ok", latency_ms=_elapsed_ms(start)
    )


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def default_probes() -> list[Probe]:
    """
    Sondas deducidas de lo que está configurado e instalado.

    Sólo se incluye lo que se puede sondear de verdad: si SQLAlchemy no está instalado,
    no hay sonda de SQL, en vez de una que siempre pasa.
    """
    probes: list[Probe] = []

    if _sql_available():
        probes.append(Probe(name="sql", check=_check_sql, timeout=2.0))
    if _redis_configured():
        # El cache es degradación, no caída: sin él la app sirve más lento.
        probes.append(
            Probe(name="redis", check=_check_redis, timeout=1.0, critical=False)
        )
    if _mongo_available():
        probes.append(Probe(name="mongo", check=_check_mongo, timeout=2.0))

    return probes


# ── Sondas ────────────────────────────────────────────────────────────────────


def _sql_available() -> bool:
    """
    Si SQLAlchemy está instalado.

    Delega en `hexcore.capabilities`, que es donde vive esta pregunta para todo el
    framework. Antes cada módulo la resolvía con su propio `try: import`, y eso tiene dos
    costos: importa el paquete de verdad —cientos de milisegundos y una entrada en
    `sys.modules`— sólo para saber si existe, y deja el import sin usar, que es ruido que
    hay que silenciar con un `noqa`.
    """
    from hexcore.capabilities import has_extra

    return has_extra("sqlalchemy")


async def _check_sql() -> None:
    from sqlalchemy import text

    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    engine = get_engine()
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


def _redis_configured() -> bool:
    """
    Si Redis está instalado **y** hay una URI configurada.

    Las dos cosas, y por eso no alcanza con `has_extra`: tener el paquete no significa que
    la app use Redis, y una sonda contra una URI que no existe reportaría caída sobre algo
    que nadie configuró.
    """
    from hexcore.capabilities import has_extra

    if not has_extra("redis"):
        return False

    from hexcore.config import LazyConfig

    return bool(getattr(LazyConfig.get_config(), "redis_uri", None))


async def _check_redis() -> None:
    from redis.asyncio import Redis

    from hexcore.config import LazyConfig

    client = Redis.from_url(LazyConfig.get_config().redis_uri)
    try:
        await client.ping()
    finally:
        await client.aclose()


def _mongo_available() -> bool:
    """Si Beanie está instalado. Ver `_sql_available` para el porqué de la delegación."""
    from hexcore.capabilities import has_extra

    return has_extra("beanie")


async def _check_mongo() -> None:
    from beanie import Document

    # Beanie guarda el cliente en el motor de cualquier documento inicializado. Si no
    # hay ninguno, no hay Mongo que sondear y la sonda no debería estar activa.
    for document in Document.__subclasses__():
        motor_collection = getattr(document, "get_motor_collection", None)
        if motor_collection is None:
            continue
        collection = motor_collection()
        await collection.database.client.admin.command("ping")
        return

    raise RuntimeError("No hay documentos Beanie inicializados que sondear.")


# ── Rutas ─────────────────────────────────────────────────────────────────────


def register_health_routes(
    app: FastAPI,
    *,
    path: str = "/health",
    probes: t.Sequence[Probe] | None = None,
    liveness: bool = True,
    readiness: bool = True,
    readiness_path: str | None = None,
    response_factory: ResponseFactory | None = None,
) -> None:
    """
    Registra `GET {path}` (liveness) y `GET {path}/ready` (readiness).

    - `{path}` responde 200 sin I/O. Apuntá aquí el liveness probe.
    - `{path}/ready` sondea las dependencias y responde **503** si algo crítico falla,
      con el detalle y la latencia por dependencia. Apuntá aquí el readiness probe.

    Las dos rutas se registran por separado a propósito. Una app que **ya publica**
    `/health` con su propia forma —y con un cliente tipado generado desde su OpenAPI— no
    puede aceptar la forma de `HealthReport` sin romper el contrato, pero sí quiere la
    readiness, que es la parte que no se puede escribir a mano en cinco minutos::

        register_health_routes(app, liveness=False)   # sólo /health/ready

    Y si lo que hace falta es conservar la forma del cuerpo, `response_factory` la
    adapta sin renunciar a las sondas::

        register_health_routes(
            app,
            response_factory=lambda r: {"ok": r.status != "down", "checks": r.dependencies},
        )

    Args:
        app: La app donde registrar.
        path: Ruta del liveness. También la base del readiness, salvo que se dé
            `readiness_path`.
        probes: Sondas del readiness. Por defecto, las de `default_probes()`.
        liveness: Registrar `GET {path}`. Poné `False` si ya publicás el tuyo.
        readiness: Registrar el readiness.
        readiness_path: Ruta del readiness. Por defecto ``f"{path}/ready"``.
        response_factory: Si se da, se le pasa el `HealthReport` y lo que devuelva es el
            cuerpo de la respuesta. El **status code** lo sigue decidiendo el informe
            (503 si algo crítico está caído), salvo que devuelvas una `Response` propia,
            en cuyo caso el status es cosa tuya.

    Raises:
        ValueError: Si se desactivan las dos rutas. Una llamada que no registra nada es
            un error de configuración, y descubrirlo por silencio cuesta un incidente.
    """
    if not liveness and not readiness:
        raise ValueError(
            "register_health_routes(liveness=False, readiness=False) no registra nada. "
            "Si no querés ninguna de las dos rutas, no llames a la función."
        )

    # Sin factory se declara `HealthReport` para que el OpenAPI documente la forma; con
    # factory el cuerpo es lo que devuelva el usuario y no hay modelo que prometer.
    response_model = None if response_factory is not None else HealthReport

    def render(report: HealthReport) -> t.Any:
        return report if response_factory is None else response_factory(report)

    # Las dos rutas de abajo las registra `@app.get` por efecto de lado, así que sus nombres
    # locales no los lee nadie y el checker las reporta como código muerto. Juntarlas acá es
    # lo que dice que están vivas, para el checker y para quien lee.
    _registradas: list[t.Any] = []

    if liveness:
        @app.get(
            path,
            tags=["health"],
            summary="Liveness: el proceso responde",
            response_model=response_model,
        )
        async def _health() -> t.Any:
            return render(await check_health(deep=False))

        # El decorador la registra en la app; el nombre local no lo lee nadie. Nombrarla
        # es lo que la separa de código muerto para el checker y para quien lee.
        _registradas.append(_health)

    if readiness:
        @app.get(
            readiness_path or f"{path}/ready",
            tags=["health"],
            summary="Readiness: las dependencias responden",
            response_model=response_model,
            responses={503: {"description": "Alguna dependencia crítica no responde"}},
        )
        async def _health_ready(response: Response) -> t.Any:
            report = await check_health(deep=True, probes=probes)
            response.status_code = report.http_status
            return render(report)

        _registradas.append(_health_ready)
