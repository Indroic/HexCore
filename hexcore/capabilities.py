"""
Qué extras están instalados, y el error cuando falta el que hace falta.

HexCore reparte sus dependencias pesadas en extras, así que media docena de módulos tiene que
preguntarse lo mismo —¿está SQLAlchemy?, ¿está Redis?— y contestar lo mismo cuando la respuesta
es que no. Antes cada uno lo resolvía a su manera: `_sql_available()` en `api/health.py`,
`_mongo_available()` al lado, un `try/except ImportError` que se comía el error en otro, y un
solo lugar —el adaptador de Procrastinate— que se acordaba de decir qué había que instalar.

El costo de esa dispersión no es la duplicación, es que **el consumidor recibe un mensaje
distinto según por dónde entró**, y en la mayoría de los casos ninguno le dice el comando. Un
`ModuleNotFoundError: No module named 'sqlalchemy'` es correcto y no sirve: no dice que HexCore
lo empaqueta bajo `[sql]`, que es lo único que el consumidor necesita saber.

Acá viven las dos operaciones:

- `has_extra(modulo)` para ramificar —un health check que sondea sólo lo que está instalado—.
- `require_extra(modulo, para=...)` para exigir, con el `pip install` adentro del mensaje.

Las dos preguntan por el **nombre importable**, no por el nombre del extra: es lo que el módulo
que llama ya sabe (necesita `sqlalchemy`, no necesita `[sql]`), y `EXTRA_DE` hace la traducción
en un solo lugar. Al revés obligaría a cada llamador a recordar el mapa, que es exactamente lo
que este módulo existe para evitar.

Uso::

    from hexcore.capabilities import has_extra, require_extra

    if has_extra("redis"):
        ...

    require_extra("sqlalchemy", para="SqlAlchemyRepository")
"""
from __future__ import annotations

import importlib.util

__all__ = ["EXTRA_DE", "has_extra", "installed_extras", "require_extra"]


#: El extra de HexCore que instala cada paquete de tercero, por nombre **importable**.
#:
#: La clave es como se escribe en un `import`, que no siempre es como se escribe en un
#: `pip install`: `argon2-cffi` se importa `argon2`, `aio-pika` se importa `aio_pika` y
#: `py_webauthn` se importa `webauthn`. Errar eso da un "no está instalado" sobre algo que sí
#: está, así que el mapa se escribe del lado del import y nunca del lado del paquete.
EXTRA_DE: dict[str, str] = {
    # ── Núcleo ────────────────────────────────────────────────────────────────
    "fastapi": "api",
    "sqlalchemy": "sql",
    "alembic": "sql",
    "asyncpg": "sql",
    "aiosqlite": "sql",
    "beanie": "mongo",
    "pymongo": "mongo",
    "redis": "redis",
    "aio_pika": "rabbitmq",
    "pika": "rabbitmq",
    "procrastinate": "procrastinate",
    "celery": "celery",
    # ── Darwin ────────────────────────────────────────────────────────────────
    "joserfc": "darwin",
    "argon2": "darwin",
    "httpx": "darwin-oauth",
    "webauthn": "darwin-passkey",
}


def has_extra(modulo: str) -> bool:
    """
    Si el paquete se puede importar.

    Args:
        modulo: El nombre importable (`"sqlalchemy"`, `"argon2"`, …).

    Returns:
        `True` si está disponible.

    Usa `find_spec` y no un `try: import`, porque importar tiene efectos: ejecuta el módulo,
    lo deja en `sys.modules` y puede costar cientos de milisegundos. Preguntar si algo está
    no debería instalarlo en el proceso.

    Los `Exception` se tragan a propósito, y no es pereza: `find_spec` levanta `ImportError`
    cuando un finder de `sys.meta_path` bloquea el módulo —que es justo lo que hacen los tests
    de dependencias opcionales— y `ValueError` cuando el módulo está a medio inicializar. En
    los dos casos la respuesta correcta a "¿lo puedo importar?" es que no.
    """
    try:
        return importlib.util.find_spec(modulo) is not None
    except Exception:
        return False


def installed_extras() -> tuple[str, ...]:
    """
    Los extras de los que hay al menos un paquete instalado, ordenados.

    Sirve para diagnósticos —un endpoint de estado, un log de arranque—, no para decidir:
    un extra puede traer varios paquetes y esto lo reporta presente con que haya uno, así
    que para ramificar preguntá por el paquete que vas a usar con `has_extra`.
    """
    return tuple(sorted({extra for mod, extra in EXTRA_DE.items() if has_extra(mod)}))


def require_extra(modulo: str, *, para: str) -> None:
    """
    Exige un paquete opcional, o levanta un `ImportError` que dice cómo instalarlo.

    Args:
        modulo: El nombre importable que hace falta.
        para: Qué es lo que lo necesita, tal como el consumidor lo nombró — la clase que
            pidió, la función que llamó. Va en la primera línea del error, y es lo que
            convierte un "falta sqlalchemy" en un "`SqlAlchemyRepository` necesita
            sqlalchemy": el consumidor no siempre sabe qué import suyo terminó acá.

    Raises:
        ImportError: si el paquete no está. El mensaje trae el `pip install` copiable.

    `ImportError` y no `RuntimeError` porque es lo que un `try/except ImportError` de un
    consumidor ya atrapa: es la excepción que Python usa para "esto no se pudo traer", y
    cambiarla obligaría a atrapar dos cosas para el mismo caso.

    Uso::

        from hexcore.capabilities import require_extra

        require_extra("celery", para="CeleryTaskEnqueuer")
    """
    if has_extra(modulo):
        return

    extra = EXTRA_DE.get(modulo)
    if extra is None:
        # Un paquete que no es de ningún extra nuestro: no podemos ofrecer el comando de
        # HexCore, así que damos el del paquete pelado en vez de inventar un extra.
        raise ImportError(
            f"{para} necesita el paquete {modulo!r}, que no está instalado.\n\n"
            f"    pip install {modulo}"
        )

    raise ImportError(
        f"{para} necesita {modulo!r}, que HexCore empaqueta en el extra [{extra}] y no está "
        f"instalado.\n\n"
        f"    pip install 'hexcore[{extra}]'"
    )
