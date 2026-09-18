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
        self._plugin_steps: list[t.Any] = []

    async def start(self) -> None:
        from hexcore.darwin.application.container import configure_identity

        contenedor = configure_identity(self._config, **self._components)
        logger.info("Darwin configurado.")

        self._verificar_defaults_de_produccion()

        if self._verify_schema:
            self._verificar_esquema()

        # `PluginRegistry.startup_steps()` existe hace varias versiones, pero nada lo llamaba
        # acá — `RbacSeedStep` (y cualquier otro paso de plugin, como el reaper de passkeys)
        # sólo corría si el consumidor lo descubría y lo agregaba a mano a su propio
        # `build_lifespan(...)` (HC-17). Se corren en el orden en que los plugins fueron
        # registrados, después de que el contenedor ya existe.
        self._plugin_steps = list(contenedor.plugins.startup_steps())
        for paso in self._plugin_steps:
            await paso.start()

    async def stop(self) -> None:
        """
        Para los pasos de los plugins y descarta el contenedor.

        Importa en un worker de vida larga que reconfigura, y en los tests: sin esto el
        registro del sobre queda apuntando a un contenedor que ya no existe.
        """
        from hexcore.darwin.application.container import reset_identity

        for paso in reversed(self._plugin_steps):
            detener = getattr(paso, "stop", None)
            if detener is not None:
                await detener()
        self._plugin_steps = []

        reset_identity()

    def _verificar_esquema(self) -> None:
        """
        Compara el metadata contra las tablas que este despliegue realmente usa.

        Cubre el núcleo **y los plugins activos**, y es acá donde la lista de plugins puede ser
        exacta: el `PluginRegistry` ya está armado, así que no hay que adivinar. Es la diferencia
        con `ensure_identity_schema_loaded`, que corre en el `env.py` sin contenedor y por eso
        necesita que le pasen los nombres.

        Esta es la red que agarra al que se olvidó de pasarlos: el síntoma de olvidarse es
        `op.drop_table` sobre una tabla con datos, y descubrir eso revisando el diff de una
        migración es demasiado tarde para confiar en que alguien lo revise.
        """
        try:
            from hexcore.darwin.infrastructure.orms.sqlalchemy.registry import (
                resolve_identity_models,
            )
            from hexcore.infrastructure.repositories.orms.sqlalchemy import Base
        except ImportError:
            # Sin el extra `[darwin-sqlalchemy]` no hay metadata que verificar.
            return

        # `t.Any` y no `type`: `__tablename__` lo agrega el mapeo declarativo en tiempo de
        # ejecución, así que no está en el tipo estático de una `type` cualquiera. Mismo
        # criterio que `identity_tables`.
        #
        # Resueltos y no `IDENTITY_MODELS`: importar `models.py` acá declaraba un segundo
        # modelo sobre `darwin_user` y rompía el arranque del consumidor que ya tenía el suyo
        # — y además verificaba las tablas por defecto en vez de las que él declaró. Ver el
        # docstring de `registry`.
        esperadas: list[t.Any] = list(resolve_identity_models(self._config_vigente()))
        esperadas.extend(self._modelos_de_los_plugins())

        registradas = set(Base.metadata.tables)
        faltan = [
            m.__tablename__ for m in esperadas if m.__tablename__ not in registradas
        ]
        if not faltan:
            return

        logger.error(
            "Estas tablas de identidad no están en Base.metadata: %s. Si usás Alembic, el "
            "próximo `revision --autogenerate` va a emitir op.drop_table sobre ellas. "
            "Importalas desde tu paquete `models/`, o llamá en el env.py a "
            "`ensure_identity_schema_loaded(plugins=%r)`.",
            ", ".join(sorted(faltan)),
            list(self._nombres_de_los_plugins()),
        )

    def _verificar_defaults_de_produccion(self) -> None:
        """
        Se niega a arrancar con los defaults de desarrollo si `debug=False`.

        Los dos que chequea son fallas **silenciosas**: la app arranca, los tests pasan, y el
        agujero aparece recién cuando hay más de un proceso. Es el mismo criterio que
        `IdentityConfig.secret_key`, que no tiene default por la misma razón — sólo que acá el
        valor por defecto no es inseguro en sí, es inseguro *en producción*.

        Falla en vez de avisar, al revés que `_verificar_esquema`. La diferencia es que ahí hay
        un caso legítimo —una app que crea sus tablas sin Alembic— y acá no: un despliegue de
        producción con la clave de firma en memoria del proceso no es una decisión, es un
        descuido.
        """
        if self._en_debug():
            return

        contenedor = self._contenedor_o_none()
        if contenedor is None:  # pragma: no cover - contenedor a medio armar
            return

        if getattr(contenedor.key_store(), "ephemeral", False):
            raise ValueError(
                "Darwin arrancaría con una clave de firma **generada en cada arranque**, y "
                "`debug` está en False.\n\n"
                "Con esto, un reload invalida todas las sesiones y, con más de un proceso, "
                "cada worker firma con una clave que los otros no verifican: el síntoma es un "
                "401 intermitente que no se reproduce en desarrollo.\n\n"
                "Sembrá la tabla de claves:\n\n"
                "    hexcore identity generate-keys --persist\n\n"
                "o inyectá el almacén: `configure_identity(config, key_store=...)`."
            )

        if self._cache_es_de_proceso():
            raise ValueError(
                "Darwin arrancaría con un `MemoryCache` y `debug` está en False.\n\n"
                "La denylist de sesiones y el contador de generación viven en ese cache, y un "
                "cache en memoria es **por proceso**: un `sign-out` corta la sesión en el "
                "worker que atendió el pedido y la deja viva en todos los demás, igual que "
                "'cerrar sesión en todos los dispositivos'.\n\n"
                "Declará un cache compartido en `ServerConfig.cache_backend` (Redis, "
                "Memcached, el que uses)."
            )

    @staticmethod
    def _cache_es_de_proceso() -> bool:
        """Si el cache configurado es el `MemoryCache` por defecto del framework."""
        try:
            from hexcore.config import LazyConfig
            from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

            return isinstance(LazyConfig.get_config().cache_backend, MemoryCache)
        except Exception:  # pragma: no cover - config a medio armar
            return False

    @staticmethod
    def _en_debug() -> bool:
        """Si la app está en modo debug. Sin config resoluble se asume producción."""
        try:
            from hexcore.config import LazyConfig

            return bool(LazyConfig.get_config().debug)
        except Exception:  # pragma: no cover - config a medio armar
            return False

    def _contenedor_o_none(self) -> t.Any:
        from hexcore.darwin.application.container import get_identity_container

        try:
            return get_identity_container()
        except Exception:  # pragma: no cover - contenedor a medio armar
            return None

    def _config_vigente(self) -> t.Any:
        """
        La config que quedó vigente, o `None` si el contenedor no llegó a armarse.

        Se lee del contenedor y no de `self._config`: `start()` ya corrió
        `configure_identity`, y el llamador puede haber pasado `None` para que la tome de
        `ServerConfig.darwin`. Acá queremos la que realmente rige.

        Tolera que no haya contenedor por el mismo motivo que `_nombres_de_los_plugins`: el
        paso se puede instanciar y llamar suelto, y un chequeo que existe para **avisar** no
        puede ser lo que impide arrancar. Sin config, `resolve_identity_models` resuelve por
        mixin, que es el comportamiento correcto en ausencia de declaración explícita.
        """
        from hexcore.darwin.application.container import get_identity_container

        try:
            return get_identity_container().config
        except Exception:  # pragma: no cover - contenedor a medio armar
            # `getattr` y no `self._config`: el paso se puede construir con `__new__` —lo hacen
            # los tests del chequeo— y ahí `__init__` no corrió.
            return getattr(self, "_config", None)

    def _nombres_de_los_plugins(self) -> tuple[str, ...]:
        """Los plugins activos, o vacío si el contenedor no llegó a armarse."""
        from hexcore.darwin.application.container import get_identity_container

        try:
            return get_identity_container().plugins.names
        except Exception:  # pragma: no cover - contenedor a medio armar
            return ()

    def _modelos_de_los_plugins(self) -> list[t.Any]:
        """
        Los modelos concretos de los plugins activos.

        Se envuelve en un `try`: un plugin de terceros puede llamarse cualquier cosa y no tener
        paquete bajo `hexcore.darwin.plugins`, y que la verificación explote sería peor que que
        no verifique — impediría arrancar por un chequeo que existe para avisar.
        """
        from hexcore.darwin.infrastructure.orms.sqlalchemy.schema import plugin_models

        try:
            return plugin_models(self._nombres_de_los_plugins())
        except Exception:  # pragma: no cover - plugin de terceros sin esquema importable
            logger.debug("No se pudieron resolver los modelos de los plugins.", exc_info=True)
            return []


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
