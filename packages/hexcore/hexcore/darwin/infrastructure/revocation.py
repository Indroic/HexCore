"""
Revocación en tres capas, con cero consultas a la base en el camino caliente.

1. **`exp` corto** (120 s). Verificación puramente criptográfica, cero I/O. El peor caso de un
   token revocado que sigue sirviendo son dos minutos.
2. **Denylist de `sid` en `ICache`** (esta clase). Una lectura de cache por petición.
3. **Contador de generación por usuario** (`GenerationGuard`). Revoca *todas* las sesiones de
   un usuario con un solo UPDATE, sin importar cuántas tenga.

Sin las tres, la revocación obliga a leer la fila de `session` en cada petición autenticada —
que es exactamente el costo que el JWT venía a evitar.

Dos decisiones que parecen detalles y no lo son:

**El vencimiento va DENTRO del valor, no en el TTL del backend.** Verificado:
`MemoryCache.set()` ignora su parámetro `expire` y nunca desaloja. Delegarle el vencimiento
haría que una revocación fuera *permanente* con el backend por defecto, y que la lista creciera
sin techo. Es el mismo motivo por el que `rate_limit` guarda su `reset_at` adentro del valor.

**Falla cerrando** (`on_cache_error="deny"`), al revés que `rate_limit`. La asimetría es
deliberada: dejar pasar una petición sin limitar es una molestia, dejar pasar un token revocado
es la vulnerabilidad que este módulo existe para evitar.
"""
from __future__ import annotations

import logging
import typing as t
from datetime import datetime
from uuid import UUID

from hexcore.darwin.domain.exceptions import TokenRevokedError
from hexcore.darwin.domain.ports import AbstractRevocationList

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.ports import AbstractClock
    from hexcore.infrastructure.cache import ICache

logger = logging.getLogger("hexcore.darwin.revocation")

__all__ = [
    "CacheErrorPolicy",
    "CacheRevocationList",
    "GenerationGuard",
]

#: Qué hacer si el backend de cache falla. ``"deny"`` es el default y el correcto para
#: revocación; ``"allow"`` existe sólo para un despliegue que prefiera disponibilidad y lo
#: declare a mano.
CacheErrorPolicy = t.Literal["allow", "deny"]

async def _tal_vez_await(valor: t.Any) -> None:
    """
    Await si hace falta.

    `ICache.set` y `ICache.delete` están declarados sync en el puerto, pero `MemoryCache` y
    `RedisCache` los implementan async. Es una inconsistencia del puerto que precede a este
    módulo; acá se absorbe en un solo lugar en vez de repetir el `hasattr` en cada llamada.
    """
    if hasattr(valor, "__await__"):
        await valor


_PREFIJO_SESION = "darwin:revoked:"
_PREFIJO_GENERACION = "darwin:gen:"


class CacheRevocationList(AbstractRevocationList):
    """
    Denylist de sesiones revocadas sobre el puerto `ICache`.

    Semántica: **se permite si no está en la lista.** Es correcto porque la entrada siempre
    cubre toda la vida restante de cualquier token que lleve ese `sid` — el `until` que se
    pasa a `revoke()` tiene que ser al menos el `exp` del token más largo en vuelo.

    Se descartó un filtro de Bloom: un falso positivo en una lista de *denegación* desloguea a
    un inocente al azar, y un Bloom plano no puede borrar, así que esa revocación espuria sería
    permanente mientras viva el filtro. El cache con vencimiento por `sid` es estrictamente
    mejor en los dos ejes.

    Uso::

        lista = CacheRevocationList(cache=config.cache_backend)

        await lista.revoke(sid, until=claims.expires_at)
        if await lista.is_revoked(sid):
            raise TokenRevokedError()
    """

    def __init__(
        self,
        *,
        cache: "ICache | None" = None,
        clock: "AbstractClock | None" = None,
        on_cache_error: CacheErrorPolicy = "deny",
        namespace: str = _PREFIJO_SESION,
    ) -> None:
        self._cache = cache
        self._namespace = namespace
        self._on_error = on_cache_error
        if clock is None:
            from hexcore.darwin.infrastructure.clock import SystemClock

            clock = SystemClock()
        self._clock = clock

    def _backend(self) -> "ICache":
        if self._cache is not None:
            return self._cache
        from hexcore.config import LazyConfig

        return LazyConfig.get_config().cache_backend

    def _clave(self, session_id: UUID) -> str:
        return f"{self._namespace}{session_id}"

    async def revoke(self, session_id: UUID, *, until: datetime) -> None:
        """
        Marca la sesión revocada hasta `until`.

        `until` tiene que cubrir el `exp` del token más largo que lleve ese `sid`. Si es más
        corto, la entrada se vuelve inerte antes de que el último token venza y ese token
        vuelve a funcionar.
        """
        backend = self._backend()
        await _tal_vez_await(
            backend.set(
                self._clave(session_id),
                {"until": until.timestamp()},
                expire=max(1, int((until - self._clock.now()).total_seconds())),
            )
        )

    async def is_revoked(self, session_id: UUID) -> bool:
        """
        Si la sesión está revocada.

        Raises:
            TokenRevokedError: si el backend falla y `on_cache_error="deny"`. Se lanza la
                excepción de revocación —y no una de infraestructura— a propósito: el
                llamador no tiene nada distinto que hacer, y el mapeo a 401 ya está.
        """
        backend = self._backend()
        try:
            estado: t.Any = await backend.get(self._clave(session_id))
        except Exception as exc:
            if self._on_error == "deny":
                logger.critical(
                    "El backend de revocación falló (%s) y on_cache_error='deny': se "
                    "rechaza el token. Mientras esto dure, ninguna sesión valida.",
                    exc,
                )
                raise TokenRevokedError(
                    "No se pudo verificar el estado de revocación de la sesión."
                ) from exc
            logger.error(
                "El backend de revocación falló (%s) y on_cache_error='allow': se acepta "
                "el token SIN verificar revocación.",
                exc,
            )
            return False

        if not isinstance(estado, dict):
            return False

        # `t.cast` y no una anotación: el narrowing de `isinstance(x, dict)` produce
        # `dict[Unknown, Unknown]`, y asignarlo a una variable anotada no lo reemplaza. El cast
        # es la afirmación explícita de que la forma la escribimos nosotros en `revoke()`.
        entrada = t.cast("dict[str, t.Any]", estado)
        until = entrada.get("until")
        if not isinstance(until, (int, float)):
            return False

        # El vencimiento se evalúa acá, no se delega al backend: `MemoryCache` ignora `expire`
        # y nunca desaloja, así que sin este chequeo la revocación sería permanente y la lista
        # crecería sin techo.
        return self._clock.now().timestamp() < until

    async def clear(self, session_id: UUID) -> None:
        """Saca la sesión de la lista. Para tests, y para deshacer una revocación errónea."""
        backend = self._backend()
        await _tal_vez_await(backend.delete(self._clave(session_id)))


class GenerationGuard:
    """
    Capa 3: revocación masiva por usuario, con un solo UPDATE.

    El token lleva `gen`; el usuario lleva `token_generation`. Si no coinciden, el token es de
    antes del corte y se rechaza. Incrementar el contador del usuario invalida **todas** sus
    sesiones a la vez — sin importar cuántas tenga y sin recorrerlas.

    Se llama en: cambio de contraseña, "cerrar todas las sesiones", cambio de rol, y alta de
    2FA. Los cuatro son momentos donde dejar viva una sesión vieja es el agujero.

    El valor se cachea (60 s por defecto) y se autopobla desde el repositorio, así que el
    camino caliente sigue sin tocar la base salvo una vez por minuto por usuario.

    ⚠️ **La ventana del cache es real**: durante hasta `ttl_seconds` después de un bump, un
    token viejo puede seguir pasando esta capa. Lo cubre la capa 2 (la denylist de `sid`, que
    se escribe en el mismo flujo) y el `exp` corto. Bajar el TTL a 0 elimina la ventana a
    costa de una consulta por petición — es una decisión de despliegue, y por eso el parámetro
    existe.

    Uso::

        guard = GenerationGuard(users=repo_usuarios, cache=cache)

        if await guard.is_stale(claims.sub, claims.gen):
            raise TokenRevokedError()
    """

    def __init__(
        self,
        *,
        users: t.Any,
        cache: "ICache | None" = None,
        clock: "AbstractClock | None" = None,
        ttl_seconds: int = 60,
        on_cache_error: CacheErrorPolicy = "deny",
        namespace: str = _PREFIJO_GENERACION,
    ) -> None:
        self._users = users
        self._cache = cache
        self._namespace = namespace
        self._ttl = ttl_seconds
        self._on_error = on_cache_error
        if clock is None:
            from hexcore.darwin.infrastructure.clock import SystemClock

            clock = SystemClock()
        self._clock = clock

    def _backend(self) -> "ICache":
        if self._cache is not None:
            return self._cache
        from hexcore.config import LazyConfig

        return LazyConfig.get_config().cache_backend

    async def current_generation(self, user_id: UUID) -> int:
        """
        La generación vigente del usuario, cacheada.

        El vencimiento va dentro del valor, por el mismo motivo que en `CacheRevocationList`.
        """
        backend = self._backend()
        clave = f"{self._namespace}{user_id}"
        ahora = self._clock.now().timestamp()

        try:
            estado: t.Any = await backend.get(clave)
        except Exception as exc:
            if self._on_error == "deny":
                logger.critical(
                    "El cache de generaciones falló (%s) y on_cache_error='deny'.", exc
                )
                raise TokenRevokedError(
                    "No se pudo verificar la generación de tokens del usuario."
                ) from exc
            logger.error("El cache de generaciones falló (%s); se lee de la base.", exc)
            estado = None

        if isinstance(estado, dict):
            entrada = t.cast("dict[str, t.Any]", estado)
            valor, hasta = entrada.get("gen"), entrada.get("until")
            if isinstance(valor, int) and isinstance(hasta, (int, float)) and ahora < hasta:
                return valor

        usuario = await self._users.get_by_id(user_id)
        generacion = int(getattr(usuario, "token_generation", 0) or 0) if usuario else 0

        await _tal_vez_await(
            backend.set(
                clave, {"gen": generacion, "until": ahora + self._ttl}, expire=self._ttl
            )
        )
        return generacion

    async def is_stale(self, user_id: UUID, token_generation: int) -> bool:
        """
        Si el token es de antes del último corte masivo.

        Compara con `<`, no con `!=`: un token con `gen` **mayor** que el del usuario es un
        token del futuro, o sea imposible salvo bug de emisión o base restaurada de un backup.
        Tratarlo como válido es lo correcto —no hay nada que revocar— y tratarlo como stale
        deslogearía a todo el mundo tras un restore.
        """
        return token_generation < await self.current_generation(user_id)

    async def invalidate_cache(self, user_id: UUID) -> None:
        """
        Descarta la generación cacheada.

        **Se llama junto con `bump_token_generation`.** Sin esto, la ventana de
        `ttl_seconds` sigue aceptando tokens viejos después de un "cerrar todas las sesiones",
        que es justo el flujo donde el usuario espera efecto inmediato.
        """
        backend = self._backend()
        await _tal_vez_await(backend.delete(f"{self._namespace}{user_id}"))
