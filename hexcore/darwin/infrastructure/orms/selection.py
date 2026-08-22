"""
Cuál backend de almacenamiento usa Darwin, y cómo se resuelve.

Darwin se parte en tres piezas instalables — `[darwin]`, `[darwin-sqlalchemy]`,
`[darwin-beanie]` — y este módulo es el único que sabe que existen las dos últimas. El
contenedor le pregunta acá y no importa ningún backend directo: así el núcleo no depende de
ninguno, que es lo que hace que la separación sea real y no una convención.

**La resolución es explícita primero y detectada después**, en ese orden y por un motivo:

1. Si `IdentityConfig.storage` está puesto, se usa ese. Punto.
2. Si no está, se mira qué extra hay instalado.
   - Uno solo → se usa ese, y es el caso cómodo del 95%.
   - **Los dos → se falla**, con la remediación copiable. Elegir uno por orden alfabético, por
     orden de import o por cualquier otra regla implícita significa que el mismo `pyproject.toml`
     puede dar un backend distinto según qué más haya instalado — y el síntoma es que la app
     arranca contra una base vacía sin que nadie note por qué.
   - Ninguno → se falla, con las dos opciones nombradas.

⚠️ El chequeo es de **instalación**, no de configuración. Que `sqlalchemy` esté instalado no
significa que el consumidor quiera guardar la identidad ahí: puede tenerlo por `[sql]` para el
resto de su app y querer Mongo para las sesiones. Ese es justamente el caso donde `storage`
explícito no es opcional, y por eso el error lo dice.
"""
from __future__ import annotations

import importlib.util
import typing as t

__all__ = ["StorageBackend", "BACKENDS", "resolve_storage_backend", "installed_backends"]

#: Los dos backends que Darwin shippea.
StorageBackend = t.Literal["sqlalchemy", "beanie"]

#: `{backend: módulo que tiene que ser importable}`.
#:
#: Se chequea el paquete de tercero y no el módulo de Darwin: `hexcore.darwin...orms.beanie`
#: existe siempre —viene en la wheel— así que su presencia no dice nada. Lo que dice si el
#: backend se puede usar es si `beanie` está instalado.
BACKENDS: dict[str, str] = {
    "sqlalchemy": "sqlalchemy",
    "beanie": "beanie",
}


def installed_backends() -> tuple[str, ...]:
    """
    Los backends cuyo paquete está instalado, en orden estable.

    Con `importlib.util.find_spec` y no con un `try: import`: importar `sqlalchemy` para
    averiguar si está cuesta ~200 ms y deja el módulo cargado en un proceso que quizá no lo
    necesitaba. `find_spec` sólo mira el `sys.path`.
    """
    return tuple(
        nombre
        for nombre, paquete in BACKENDS.items()
        if importlib.util.find_spec(paquete) is not None
    )


def resolve_storage_backend(preferido: str | None = None) -> StorageBackend:
    """
    El backend a usar.

    Args:
        preferido: Lo que dice `IdentityConfig.storage`, o `None` para detectar.

    Returns:
        `"sqlalchemy"` o `"beanie"`.

    Raises:
        ValueError: el nombre pedido no existe, el extra pedido no está instalado, no hay
            ninguno, o hay dos y no se eligió. Los cuatro mensajes traen la remediación.

    Uso::

        from hexcore.darwin.infrastructure.orms.selection import resolve_storage_backend

        backend = resolve_storage_backend("beanie")
        assert backend == "beanie"
    """
    disponibles = installed_backends()

    if preferido is not None:
        if preferido not in BACKENDS:
            raise ValueError(
                f"`IdentityConfig.storage={preferido!r}` no existe. Los backends de Darwin son "
                f"{', '.join(repr(b) for b in BACKENDS)}."
            )
        if preferido not in disponibles:
            raise ValueError(
                f"`IdentityConfig.storage={preferido!r}` pero el paquete "
                f"{BACKENDS[preferido]!r} no está instalado.\n\n"
                f"    pip install 'hexcore[darwin-{preferido}]'"
            )
        return t.cast(StorageBackend, preferido)

    # `len(...) == 0` y no `not disponibles`: pyright estrecha el largo de una tupla por
    # `len()` y no por veracidad, así que con `not` el `disponibles[0]` del final queda
    # reportado como índice fuera de rango sobre `tuple[()]`.
    if len(disponibles) == 0:
        raise ValueError(
            "Darwin necesita un backend de almacenamiento y no hay ninguno instalado.\n\n"
            "Elegí uno:\n\n"
            "    pip install 'hexcore[darwin-sqlalchemy]'   # PostgreSQL, SQLite, MySQL\n"
            "    pip install 'hexcore[darwin-beanie]'       # MongoDB\n"
        )

    if len(disponibles) > 1:
        # Ver el docstring del módulo: elegir por una regla implícita hace que el backend dependa
        # de qué más haya instalado, y el síntoma es una app que arranca contra una base vacía.
        raise ValueError(
            f"Hay más de un backend de almacenamiento instalado "
            f"({', '.join(disponibles)}) y Darwin no elige por vos: el que quede afuera se "
            f"nota recién cuando la app arranca contra una base vacía.\n\n"
            f"Declaralo:\n\n"
            f'    IdentityConfig(storage="{disponibles[0]}", ...)\n\n'
            f"o por entorno:\n\n"
            f"    HEXCORE_DARWIN_STORAGE={disponibles[0]}\n"
        )

    # Los dos `if` de arriba descartaron "ninguno" y "más de uno": acá hay exactamente uno.
    return t.cast(StorageBackend, disponibles[0])
