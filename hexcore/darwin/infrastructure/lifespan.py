"""
Pasos de arranque de Darwin, para `build_lifespan(...)`.

Los tres cumplen el protocolo `StartupStep` de HexCore (`name` + `async start`, con `stop`
opcional), así que se componen con los que ya existen —`SqlEngineStep`, `CacheStep`,
`CronSeedStep`— sin ningún mecanismo nuevo.

El orden importa y no es arbitrario: `IdentityStep` va **después** de `SqlEngineStep`, porque
validar el esquema necesita el engine.
"""
from __future__ import annotations

import logging
import typing as t
from datetime import timedelta

if t.TYPE_CHECKING:
    from hexcore.darwin.application.config import IdentityConfig

logger = logging.getLogger("hexcore.darwin.lifespan")

__all__ = ["IdentityStep", "SessionReaperStep", "identity_startup_steps"]


class IdentityStep:
    """
    Configura Darwin y verifica que su esquema esté visible para Alembic.

    Dos cosas, y la segunda es la que salva datos: chequea que las tablas de identidad estén
    en `Base.metadata`. Si no lo están —porque el consumidor no las importa desde su paquete
    `models/`— `alembic revision --autogenerate` las va a ver ausentes y va a emitir
    `op.drop_table` sobre el almacén de credenciales completo. Es el modo de falla de §5.3
    del documento de arquitectura, y ya ocurre hoy con `hexcore_cron_jobs`.

    Avisa en vez de fallar: una app que crea sus tablas con `create_identity_tables()` y no
    usa Alembic es un caso legítimo, y tumbar su arranque sería un falso positivo. El aviso es
    `logger.error` porque la consecuencia, cuando aplica, es pérdida de datos.

    Uso::

        lifespan = build_lifespan(
            SqlEngineStep(),
            IdentityStep(IdentityConfig()),
        )
    """

    name = "darwin-identity"

    def __init__(
        self,
        config: "IdentityConfig | None" = None,
        *,
        components: t.Mapping[str, t.Any] | None = None,
        verify_schema: bool = True,
    ) -> None:
        self._config = config
        self._components = dict(components or {})
        self._verify_schema = verify_schema

    async def start(self) -> None:
        from hexcore.darwin.application.container import configure_identity

        configure_identity(self._config, **self._components)
        logger.info("Darwin configurado.")

        if self._verify_schema:
            self._verificar_esquema()

    async def stop(self) -> None:
        """
        Descarta el contenedor y deregistra el sobre.

        Importa en un worker de vida larga que reconfigura, y en los tests: sin esto el
        registro del sobre queda apuntando a un contenedor que ya no existe.
        """
        from hexcore.darwin.application.container import reset_identity

        reset_identity()

    def _verificar_esquema(self) -> None:
        try:
            from hexcore.darwin.infrastructure.models import IDENTITY_MODELS
            from hexcore.infrastructure.repositories.orms.sqlalchemy import Base
        except ImportError:
            # Sin el extra `[sql]` no hay metadata que verificar.
            return

        registradas = set(Base.metadata.tables)
        faltan = [
            m.__tablename__ for m in IDENTITY_MODELS if m.__tablename__ not in registradas
        ]
        if not faltan:
            return

        logger.error(
            "Estas tablas de identidad no están en Base.metadata: %s. Si usás Alembic, el "
            "próximo `revision --autogenerate` va a emitir op.drop_table sobre ellas. "
            "Importalas desde tu paquete `models/` (o llamá a "
            "`ensure_identity_schema_loaded()` en el env.py).",
            ", ".join(sorted(faltan)),
        )


class SessionReaperStep:
    """
    Borra sesiones y verificaciones vencidas al arrancar.

    **Al arrancar y no periódicamente**, y es una limitación consciente: un barrido periódico
    es un cron, y HexCore ya tiene uno (`DynamicScheduler`) — meter un `asyncio.Task` acá
    sería un segundo planificador que nadie configura y que corre una vez por réplica.

    Para el barrido continuo, registrá `reap_expired_sessions` como `@background_task` y
    programalo con `cron_job`. Este paso existe para que una app chica no tenga que montar
    nada.

    `on_error` es `"warn"` por defecto: no poder limpiar filas viejas no debería impedir que
    la aplicación arranque.
    """

    name = "darwin-session-reaper"

    def __init__(self, *, grace: timedelta = timedelta(days=7)) -> None:
        self._grace = grace

    async def start(self) -> None:
        from hexcore.darwin.application.container import get_identity_container

        contenedor = get_identity_container()
        limite = contenedor.clock().now() - self._grace

        sesiones = await contenedor.sessions_repository().delete_expired(before=limite)
        verificaciones = await contenedor.verifications().delete_expired(before=limite)

        if sesiones or verificaciones:
            logger.info(
                "Reaper: %d sesiones y %d verificaciones vencidas borradas.",
                sesiones,
                verificaciones,
            )


def identity_startup_steps(
    config: "IdentityConfig | None" = None,
    *,
    components: t.Mapping[str, t.Any] | None = None,
    reap: bool = True,
) -> list[t.Any]:
    """
    Los pasos de Darwin en el orden correcto, para desempaquetar en `build_lifespan`.

    Uso::

        lifespan = build_lifespan(
            SqlEngineStep(),
            *identity_startup_steps(IdentityConfig()),
        )
    """
    pasos: list[t.Any] = [IdentityStep(config, components=components)]
    if reap:
        pasos.append(SessionReaperStep())
    return pasos
