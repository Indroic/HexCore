"""
Event store sobre Redis Streams. Requiere el extra ``[redis]``.

**No comparte código con `RedisEventBus`, y es deliberado.** El bus publica notificaciones:
efímeras, con consumer groups y `xack`, y lo que importa es que lleguen. El store persiste
hechos: permanentes, con versión y orden, y lo que importa es que no se pierdan ni se
dupliquen. Comparten el servidor Redis, no la semántica.
"""
from __future__ import annotations

import json
import typing as t
from datetime import UTC, datetime
from uuid import UUID

from hexcore.capabilities import require_extra

require_extra("redis", para="el event store sobre Redis")

from hexcore.domain.cqrs.resolution import build_fqn  # noqa: E402
from hexcore.domain.events import DomainEvent  # noqa: E402
from hexcore.domain.eventsourcing.exceptions import ConcurrencyError  # noqa: E402
from hexcore.domain.eventsourcing.projections import (  # noqa: E402
    AbstractCheckpointStore,
)
from hexcore.domain.eventsourcing.store import AbstractEventStore  # noqa: E402
from hexcore.domain.eventsourcing.stored import (  # noqa: E402
    EXPECTED_VERSION_ANY,
    StoredEvent,
)
from hexcore.infrastructure.eventsourcing.memory import stream_type_de  # noqa: E402

if t.TYPE_CHECKING:
    from redis.asyncio import Redis

    from hexcore.domain.cqrs.serializer import AbstractSerializer

__all__ = ["RedisEventStore", "RedisCheckpointStore", "SCRIPT_DE_APPEND"]

#: El script que hace atómico el append.
#:
#: Redis ejecuta un script Lua como **una unidad**: nada se intercala. Es la única forma de
#: que "validar la versión + reservar posiciones + escribir N eventos" sea una sola
#: operación; con comandos sueltos, dos escritores se intercalan entre la validación y la
#: escritura y los dos pasan.
#:
#: La unicidad por versión sale de un detalle de `XADD`: **con un id explícito, Redis rechaza
#: cualquier id menor o igual al último del stream**. Usando `<version>-0` como id, la versión
#: del agregado *es* la posición en el stream, y el segundo escritor recibe el error del
#: propio Redis. Concurrencia optimista real, sin `WATCH` y sin locks.
#:
#: KEYS[1] = stream del agregado · KEYS[2] = stream global · KEYS[3] = contador de posiciones
#: ARGV[1] = expected_version · ARGV[2] = EXPECTED_VERSION_ANY · ARGV[3] = cantidad
#: ARGV[4..] = los payloads, uno por evento
SCRIPT_DE_APPEND = """
local ultimo = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
local version_actual = 0
if #ultimo > 0 then
    local id = ultimo[1][1]
    version_actual = tonumber(string.match(id, '^(%d+)'))
end

local esperada = tonumber(ARGV[1])
local cualquiera = tonumber(ARGV[2])
if esperada ~= cualquiera and version_actual ~= esperada then
    return {err = 'HEXCORE_CONCURRENCY:' .. version_actual}
end

local cuantos = tonumber(ARGV[3])
local ultima_posicion = redis.call('INCRBY', KEYS[3], cuantos)
local primera_posicion = ultima_posicion - cuantos + 1

local resultado = {}
for i = 1, cuantos do
    local version = version_actual + i
    local posicion = primera_posicion + i - 1
    local payload = ARGV[3 + i]
    -- El id explícito es lo que da la unicidad por versión. Si otro escritor gano la
    -- carrera entre el XREVRANGE y este XADD, Redis rechaza el id y el script aborta:
    -- nada de lo escrito hasta aca queda, porque el script es atomico.
    redis.call('XADD', KEYS[1], version .. '-0', 'p', payload, 'g', posicion, 'v', version)
    -- El id del stream global es autogenerado ('*'), asi que la version no se puede
    -- derivar de el: viaja como campo 'v' en las dos escrituras, y `_a_stored` la lee de
    -- ahi sin importar de que stream venga la entrada.
    redis.call('XADD', KEYS[2], '*', 'p', payload, 'g', posicion, 'v', version)
    resultado[i] = {version, posicion}
end

return cjson.encode(resultado)
"""


def _ahora() -> datetime:
    return datetime.now(UTC)


class RedisEventStore(AbstractEventStore):
    """
    Event store sobre Redis Streams: un stream por agregado más uno global.

    ## Lo que hay que saber antes de elegirlo

    1. **Redis es memoria.** Un event store acá exige como mínimo AOF con
       `appendfsync everysec`, y aun así se puede perder el último segundo de escrituras. Con
       sólo RDB se pierden minutos. Para el almacén primario de un sistema event-sourced, el
       adaptador recomendado es el de SQL; éste sirve para quien ya tiene Redis y quiere un
       log rápido de vida corta, o un buffer caliente para alimentar proyecciones.
    2. **`maxmemory-policy allkeys-lru` borra streams enteros.** El adaptador nunca pasa
       `MAXLEN` en el `XADD` —recortar un log de eventos es destruirlo— pero no puede
       protegerse de una política del servidor que desaloje sus claves.
    3. **`read_all(stream_types=...)` es un scan con filtro en el cliente.** Un stream de
       Redis no tiene índices secundarios: la categoría se filtra después de leer.
    """

    def __init__(
        self,
        redis_client: "Redis",
        *,
        serializer: "AbstractSerializer | None" = None,
        prefix: str = "es",
    ) -> None:
        super().__init__(serializer)
        self._redis = redis_client
        self._prefix = prefix
        self._script: t.Any = None

    # ── Claves ────────────────────────────────────────────────────────────────
    def _clave_de_stream(self, stream_id: str) -> str:
        return f"{self._prefix}:stream:{stream_id}"

    @property
    def _clave_global(self) -> str:
        return f"{self._prefix}:$all"

    @property
    def _clave_de_posicion(self) -> str:
        return f"{self._prefix}:position"

    # ── Escritura ─────────────────────────────────────────────────────────────
    async def append(
        self,
        stream_id: str,
        events: t.Sequence[DomainEvent],
        *,
        expected_version: int,
        stream_type: str | None = None,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> list[StoredEvent]:
        if not events:
            return []

        categoria = stream_type or stream_type_de(stream_id)
        momento = _ahora()

        payloads: list[str] = []
        for evento in events:
            payloads.append(
                json.dumps(
                    {
                        "event_id": str(evento.event_id),
                        "stream_id": stream_id,
                        "stream_type": categoria,
                        "event_type": build_fqn(type(evento)),
                        "payload": self._serializer.serialize_envelope(evento, metadata),
                        "occurred_on": evento.occurred_on.isoformat(),
                        "recorded_at": momento.isoformat(),
                    }
                )
            )

        script = self._registrar_script()
        try:
            crudo = await script(
                keys=[
                    self._clave_de_stream(stream_id),
                    self._clave_global,
                    self._clave_de_posicion,
                ],
                args=[expected_version, EXPECTED_VERSION_ANY, len(events), *payloads],
            )
        except Exception as error:
            actual = self._version_del_error(error)
            if actual is None:
                raise
            raise ConcurrencyError(stream_id, expected_version, actual) from error

        asignados = json.loads(crudo if isinstance(crudo, str) else crudo.decode())
        return [
            StoredEvent(
                stream_id=stream_id,
                stream_type=categoria,
                version=version,
                global_position=posicion,
                event_id=evento.event_id,
                event_type=build_fqn(type(evento)),
                payload=json.loads(payloads[indice])["payload"],
                occurred_on=evento.occurred_on,
                recorded_at=momento,
            )
            for indice, ((version, posicion), evento) in enumerate(
                zip(asignados, events, strict=True)
            )
        ]

    # ── Lectura ───────────────────────────────────────────────────────────────
    async def read_stream(
        self,
        stream_id: str,
        *,
        from_version: int = 1,
        to_version: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        desde = f"{from_version}-0"
        hasta = f"{to_version}-0" if to_version is not None else "+"

        entradas = await self._redis.xrange(
            self._clave_de_stream(stream_id), min=desde, max=hasta, count=limit
        )
        return [self._a_stored(entrada) for entrada in entradas]

    async def read_all(
        self,
        *,
        from_position: int = 0,
        limit: int = 500,
        stream_types: t.Sequence[str] | None = None,
    ) -> list[StoredEvent]:
        categorias = set(stream_types) if stream_types else None
        seleccion: list[StoredEvent] = []
        cursor = "-"

        # Se lee de a bloques y se filtra en el cliente, porque un stream de Redis no tiene
        # índices secundarios ni forma de saltar a una posición global. Es la limitación 3
        # del docstring de la clase.
        while len(seleccion) < limit:
            entradas = await self._redis.xrange(
                self._clave_global, min=cursor, max="+", count=limit * 2
            )
            if not entradas:
                break

            for entrada in entradas:
                persistido = self._a_stored(entrada)
                if persistido.global_position <= from_position:
                    continue
                if categorias is not None and persistido.stream_type not in categorias:
                    continue
                seleccion.append(persistido)
                if len(seleccion) >= limit:
                    break

            siguiente = self._siguiente_cursor(entradas[-1][0])
            if siguiente == cursor:
                break
            cursor = siguiente

        return seleccion

    async def stream_version(self, stream_id: str) -> int:
        ultimo = await self._redis.xrevrange(
            self._clave_de_stream(stream_id), max="+", min="-", count=1
        )
        if not ultimo:
            return 0
        return self._version_del_id(ultimo[0][0])

    # ── Interno ───────────────────────────────────────────────────────────────
    def _registrar_script(self) -> t.Any:
        """
        Registra el script una vez y reusa su SHA.

        `register_script` no habla con Redis: devuelve un callable que usa `EVALSHA` y cae a
        `EVAL` la primera vez o si el servidor perdió el script (un `SCRIPT FLUSH`, un
        reinicio). Cachearlo evita reenviar el cuerpo en cada `append`.
        """
        if self._script is None:
            self._script = self._redis.register_script(SCRIPT_DE_APPEND)
        return self._script

    @staticmethod
    def _version_del_error(error: Exception) -> int | None:
        """
        La versión real si el error es un conflicto, o `None` si es otra cosa.

        Dos formas de conflicto, y las dos importan. El script devuelve
        `HEXCORE_CONCURRENCY:<version>` cuando la comprobación explícita falla. Y Redis
        rechaza el `XADD` por su cuenta cuando el id ya existe —la carrera que la
        comprobación no puede cubrir—, con un mensaje que habla de un id "equal or smaller".

        Ante cualquier otro error se devuelve `None` y el llamador lo propaga: traducir todo a
        `ConcurrencyError` haría que se reintentara para siempre algo que no se arregla
        reintentando.
        """
        mensaje = str(error)
        if "HEXCORE_CONCURRENCY:" in mensaje:
            _, _, cola = mensaje.partition("HEXCORE_CONCURRENCY:")
            digitos = "".join(c for c in cola if c.isdigit())
            return int(digitos) if digitos else 0

        if "equal or smaller" in mensaje.lower():
            # Redis no dice cuál es la versión actual en este error; el llamador la relee si
            # la necesita. 0 sería mentir menos que inventar un número.
            return 0

        return None

    @staticmethod
    def _version_del_id(id_de_entrada: t.Any) -> int:
        texto = (
            id_de_entrada.decode()
            if isinstance(id_de_entrada, bytes)
            else str(id_de_entrada)
        )
        return int(texto.split("-", 1)[0])

    @staticmethod
    def _siguiente_cursor(id_de_entrada: t.Any) -> str:
        """El id siguiente al dado, para paginar `xrange` sin repetir la última entrada."""
        texto = (
            id_de_entrada.decode()
            if isinstance(id_de_entrada, bytes)
            else str(id_de_entrada)
        )
        milisegundos, _, secuencia = texto.partition("-")
        return f"{milisegundos}-{int(secuencia) + 1}"

    @staticmethod
    def _campo(campos: t.Mapping[t.Any, t.Any], nombre: str) -> t.Any:
        """
        Lee un campo sin depender de si el cliente decodifica las respuestas.

        `redis.asyncio` devuelve `bytes` salvo que se lo haya construido con
        `decode_responses=True`. Un adaptador que asuma una de las dos formas funciona con la
        mitad de las configuraciones, y falla con un `KeyError` que no explica nada.
        """
        if nombre.encode() in campos:
            return campos[nombre.encode()]
        return campos[nombre]

    def _a_stored(self, entrada: t.Any) -> StoredEvent:
        _id, campos = entrada
        crudo = self._campo(campos, "p")
        datos = json.loads(crudo if isinstance(crudo, str) else crudo.decode())

        return StoredEvent(
            stream_id=datos["stream_id"],
            stream_type=datos["stream_type"],
            version=int(self._campo(campos, "v")),
            global_position=int(self._campo(campos, "g")),
            event_id=UUID(datos["event_id"]),
            event_type=datos["event_type"],
            payload=datos["payload"],
            occurred_on=datetime.fromisoformat(datos["occurred_on"]),
            recorded_at=datetime.fromisoformat(datos["recorded_at"]),
        )


class RedisCheckpointStore(AbstractCheckpointStore):
    """
    Checkpoints en un hash de Redis, uno por suscripción.

    Un hash y no una clave por suscripción: leer el avance de todas juntas es una sola
    operación, que es lo que quiere un endpoint de diagnóstico —"¿cuánto le falta a cada
    proyección?"— y lo que con claves sueltas obligaría a un `SCAN`.

    Los tres métodos silencian `reportGeneralTypeIssues` en la línea exacta: los stubs de
    `redis-py` declaran los comandos de hash con el tipo de retorno del cliente **síncrono**,
    así que pyright cree que `await` se aplica sobre un `str` o un `int`. En runtime el
    cliente async devuelve corrutinas y el `await` es correcto; el problema es del stub, no
    del código. Se silencia la regla puntual y no con un `# type: ignore` pelado, que además
    taparía un error real de tipos en la misma llamada.
    """

    def __init__(self, redis_client: "Redis", *, prefix: str = "es") -> None:
        self._redis = redis_client
        self._clave = f"{prefix}:checkpoints"

    async def load(self, subscription: str) -> int:
        valor = await self._redis.hget(  # pyright: ignore[reportGeneralTypeIssues, reportUnknownVariableType]
            self._clave, subscription
        )
        return int(valor) if valor is not None else 0  # pyright: ignore[reportUnknownArgumentType]

    async def save(self, subscription: str, position: int) -> None:
        await self._redis.hset(  # pyright: ignore[reportGeneralTypeIssues, reportUnknownMemberType]
            self._clave, subscription, str(position)
        )

    async def reset(self, subscription: str) -> None:
        await self._redis.hdel(  # pyright: ignore[reportGeneralTypeIssues]
            self._clave, subscription
        )
