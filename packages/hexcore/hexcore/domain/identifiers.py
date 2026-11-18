"""
Qué versión de UUID usan los identificadores que genera el framework.

Hasta 11.6 todo id nuevo era `uuid4()`, sin forma de cambiarlo. Eso es un buen default
(aleatorio puro, nada que filtrar), pero para una clave primaria en una tabla grande es la peor
opción: los v4 no tienen orden, así que cada insert cae en un lugar al azar del índice B-tree,
lo parte y lo desordena. Los **v7** (RFC 9562) llevan un timestamp en los bits altos y se
insertan siempre al final; son la elección habitual para PKs. Este módulo permite elegir, en
dos niveles:

- **Global**: `set_default_uuid_version(7)`, o la variable de entorno `HEXCORE_UUID_VERSION=7`.
  Lo lee todo lo que el framework genera sin que nadie lo pida: `BaseEntity.id`,
  `AggregateRoot.id`, `DomainEvent.event_id`, `Command.command_id`, el `id` de
  `hexcore.sql.BaseModel` y los ids de las entidades y tablas de Darwin.
- **Por clase**: `class Pedido(BaseEntity, uuid_version=7)` y
  `class PedidoModel(BaseModel[Pedido], uuid_version=7)`. Gana sobre el global y lo heredan las
  subclases.

El global se **resuelve en cada llamada**, no al definir la clase: cambiarlo después de importar
los modelos afecta a los ids que se generen a partir de ahí. El por-clase se fija al definirla.

Cambiar la versión **no toca los ids existentes**: un UUID es un UUID, la columna y el campo
siguen siendo `UUID`, y v4 y v7 conviven en la misma tabla.

Sólo se pueden generar las versiones que no necesitan datos de entrada: 1, 4, 6 y 7. Las 3 y 5
son hashes de un nombre dentro de un espacio de nombres (`uuid.uuid3` / `uuid.uuid5`), y la 8
es de formato libre; no hay qué generar «a secas», así que se rechazan con un mensaje que lo
dice en vez de inventar un nombre.

Este módulo es dominio puro: sólo usa la biblioteca estándar.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
import typing as t
import uuid
from uuid import UUID

__all__ = [
    "DEFAULT_UUID_VERSION",
    "ENV_UUID_VERSION",
    "SUPPORTED_UUID_VERSIONS",
    "UUIDVersion",
    "UnsupportedUUIDVersionError",
    "apply_uuid_version_to_field",
    "get_default_uuid_version",
    "new_uuid",
    "set_default_uuid_version",
    "uuid_factory",
    "using_uuid_version",
    "validate_uuid_version",
]

UUIDVersion = t.Literal[1, 4, 6, 7]

SUPPORTED_UUID_VERSIONS: tuple[int, ...] = (1, 4, 6, 7)

#: Lo que se genera si nadie eligió nada. Se mantiene en 4 a propósito: cambiarlo sería un
#: cambio de comportamiento para todo el que actualice sin tocar nada.
DEFAULT_UUID_VERSION: UUIDVersion = 4

#: Variable de entorno que fija el default global. Sólo se lee si `set_default_uuid_version`
#: no se llamó, así que el código gana sobre el entorno.
ENV_UUID_VERSION = "HEXCORE_UUID_VERSION"


class UnsupportedUUIDVersionError(ValueError):
    """Se pidió una versión de UUID que el framework no puede generar sin más datos."""


# ── Validación ────────────────────────────────────────────────────────────────


def validate_uuid_version(version: t.Any) -> UUIDVersion:
    """
    Devuelve `version` como `UUIDVersion` o lanza `UnsupportedUUIDVersionError`.

    Acepta el entero (`7`) o su texto (`"7"`, que es como llega de una variable de entorno).
    Un `bool` se rechaza aunque sea un `int`: `True` sería la versión 1 sin que nadie lo
    haya querido.
    """
    if isinstance(version, bool):
        raise UnsupportedUUIDVersionError(
            f"uuid_version no puede ser un bool ({version!r}); usa uno de "
            f"{SUPPORTED_UUID_VERSIONS}."
        )
    try:
        numero = int(version)
    except (TypeError, ValueError):
        raise UnsupportedUUIDVersionError(
            f"uuid_version={version!r} no es un numero de version; usa uno de "
            f"{SUPPORTED_UUID_VERSIONS}."
        ) from None

    if numero in (3, 5):
        raise UnsupportedUUIDVersionError(
            f"UUID v{numero} es el hash de un nombre dentro de un espacio de nombres "
            f"(uuid.uuid{numero}); no hay un id que generar sin esos datos. Usa uno de "
            f"{SUPPORTED_UUID_VERSIONS}, o genera el id tu mismo y pasalo como `id=`."
        )
    if numero not in SUPPORTED_UUID_VERSIONS:
        raise UnsupportedUUIDVersionError(
            f"uuid_version={numero} no esta soportada; usa uno de {SUPPORTED_UUID_VERSIONS}."
        )
    return t.cast(UUIDVersion, numero)


# ── Generadores ───────────────────────────────────────────────────────────────

_VARIANTE_RFC = 0b10


def _uuid6() -> UUID:
    """
    UUID v6: el v1 con los bytes del timestamp reordenados para que ordene por tiempo.

    `uuid.uuid6` existe desde 3.14; el paquete soporta 3.12, de ahí el fallback. Se arma desde
    un `uuid1` (que ya resuelve el reloj, el `clock_seq` y el nodo) en vez de rehacerlo.
    """
    nativo = getattr(uuid, "uuid6", None)
    if nativo is not None:
        return nativo()

    base = uuid.uuid1()
    marca = base.time  # 60 bits, intervalos de 100 ns desde 1582-10-15
    valor = (
        ((marca >> 12) << 80)  # time_high + time_mid, 48 bits
        | (6 << 76)
        | ((marca & 0xFFF) << 64)  # time_low, 12 bits
        | (_VARIANTE_RFC << 62)
        | (base.clock_seq << 48)
        | base.node
    )
    return UUID(int=valor)


_candado_v7 = threading.Lock()
_ultimo_ms_v7 = 0
_contador_v7 = 0


def _uuid7() -> UUID:
    """
    UUID v7 (RFC 9562): 48 bits de milisegundos Unix, y el resto aleatorio.

    `uuid.uuid7` existe desde 3.14. El fallback usa el «método 1» del RFC: los 12 bits de
    `rand_a` son un contador que arranca al azar en cada milisegundo nuevo y sube dentro del
    mismo, de modo que **dos ids generados en el mismo ms salen en orden creciente** en vez de
    al azar. Eso importa para lo que justifica usar v7: que el índice reciba inserts al final.
    Si el contador se desborda se adelanta el reloj un ms, que es lo que el RFC prescribe.
    """
    nativo = getattr(uuid, "uuid7", None)
    if nativo is not None:
        return nativo()

    global _ultimo_ms_v7, _contador_v7
    with _candado_v7:
        ahora = time.time_ns() // 1_000_000
        if ahora > _ultimo_ms_v7:
            _ultimo_ms_v7 = ahora
            # Se deja el bit alto en 0 para tener margen antes del desborde.
            _contador_v7 = int.from_bytes(os.urandom(2), "big") & 0x7FF
        else:
            _contador_v7 += 1
            if _contador_v7 > 0xFFF:
                _ultimo_ms_v7 += 1
                _contador_v7 = int.from_bytes(os.urandom(2), "big") & 0x7FF
        milisegundos, contador = _ultimo_ms_v7, _contador_v7

    aleatorio = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    valor = (
        (milisegundos << 80)
        | (7 << 76)
        | (contador << 64)
        | (_VARIANTE_RFC << 62)
        | aleatorio
    )
    return UUID(int=valor)


_GENERADORES: dict[int, t.Callable[[], UUID]] = {
    1: uuid.uuid1,
    4: uuid.uuid4,
    6: _uuid6,
    7: _uuid7,
}


# ── Default global ────────────────────────────────────────────────────────────

# `None` = nadie lo fijó por codigo; entonces manda el entorno y, sin entorno, el default.
_version_global: UUIDVersion | None = None


def get_default_uuid_version() -> UUIDVersion:
    """
    La versión que se usa cuando una clase no pidió la suya.

    Orden: lo fijado con `set_default_uuid_version`, luego `HEXCORE_UUID_VERSION`, luego 4.
    El entorno se lee en cada llamada y no se cachea, así que `monkeypatch.setenv` en un
    test funciona; un valor inválido falla acá, con el nombre de la variable en el mensaje.
    """
    if _version_global is not None:
        return _version_global

    crudo = os.environ.get(ENV_UUID_VERSION, "").strip()
    if not crudo:
        return DEFAULT_UUID_VERSION
    try:
        return validate_uuid_version(crudo)
    except UnsupportedUUIDVersionError as exc:
        raise UnsupportedUUIDVersionError(f"{ENV_UUID_VERSION}: {exc}") from None


def set_default_uuid_version(version: UUIDVersion | int | None) -> None:
    """
    Fija la versión global. `None` la quita y vuelve al entorno / default.

    Afecta a los ids que se generen **desde ahora**, incluso los de clases ya definidas que
    no pidieron una versión propia.
    """
    global _version_global
    _version_global = None if version is None else validate_uuid_version(version)


@contextlib.contextmanager
def using_uuid_version(version: UUIDVersion | int) -> t.Iterator[None]:
    """
    Cambia la versión global dentro del `with` y la restaura al salir.

    Pensado para tests: el cambio es de **proceso**, no de hilo ni de tarea, así que no es
    para usarlo en código de producción concurrente.
    """
    global _version_global
    anterior = _version_global
    _version_global = validate_uuid_version(version)
    try:
        yield
    finally:
        _version_global = anterior


# ── Versión por clase (pydantic) ──────────────────────────────────────────────


def apply_uuid_version_to_field(
    model: t.Any, field_name: str, version: UUIDVersion | int | None
) -> None:
    """
    Cambia el `default_factory` del campo `field_name` de un modelo pydantic, y lo reconstruye.

    Lo llama `__pydantic_init_subclass__` de las bases que aceptan `uuid_version=` como
    argumento de clase. Con `version=None` no hace nada: la clase conserva lo que heredó, que
    es el default global dinámico o la versión fija de un ancestro.

    Se muta el `FieldInfo` de la **subclase** (pydantic copia los de las bases al construirla),
    así que el padre y las demás ramas de la jerarquía no se enteran.
    """
    if version is None:
        return
    model.model_fields[field_name].default_factory = uuid_factory(version)
    model.model_rebuild(force=True)


# ── API de generación ─────────────────────────────────────────────────────────


def new_uuid(version: UUIDVersion | int | None = None) -> UUID:
    """
    Un UUID nuevo. Sin `version`, usa el default global.

    Es lo que reemplaza a `uuid4()` en todo el framework; úsalo en tu código si quieres que
    tus ids sigan la configuración.
    """
    if version is None:
        version = get_default_uuid_version()
    else:
        version = validate_uuid_version(version)
    return _GENERADORES[version]()


def _nuevo_uuid_global() -> UUID:
    # Sin parametros a proposito: `new_uuid(version=None)` tiene uno, y tanto pydantic (que
    # le pasa los datos ya validados a un `default_factory` de un argumento) como SQLAlchemy
    # (que le pasa el contexto de ejecucion) lo llamarian con algo en `version`.
    return _GENERADORES[get_default_uuid_version()]()


def uuid_factory(version: UUIDVersion | int | None = None) -> t.Callable[[], UUID]:
    """
    Un callable sin argumentos que genera UUIDs: el `default_factory` de un campo.

    - `version=None` → **dinámico**: consulta el default global en cada llamada.
    - `version=7` → **fijo**: valida ahora (falla al definir la clase, no al insertar) y
      genera siempre esa versión.
    """
    if version is None:
        return _nuevo_uuid_global

    fija = validate_uuid_version(version)
    generador = _GENERADORES[fija]

    def _nuevo_uuid() -> UUID:
        return generador()

    _nuevo_uuid.__name__ = f"new_uuid_v{fija}"
    return _nuevo_uuid
