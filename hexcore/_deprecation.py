"""
Mecanismo de deprecación de la superficie de API anterior a 5.0.

Conviven dos nombres para varios conceptos (`AbstractCommandBus`/`ICommandBus`,
`AbstractSerializer`/`ISerializer`, …) por retrocompatibilidad con 1.x/2.x. Los canónicos
son los `Abstract*`; los alias siguen funcionando, pero avisan y se eliminarán en 6.0.

Un alias declarado como `ICommandBus = AbstractCommandBus` **no puede avisar**: es una
asignación, y leerlo no ejecuta nada. Por eso el aviso se implementa con `__getattr__` de
módulo (PEP 562), que sólo se invoca cuando el nombre no está en los globals del módulo —
o sea, exactamente al pedir el alias.

Uso en el módulo que declara los alias::

    from hexcore._deprecation import deprecated_aliases

    __getattr__ = deprecated_aliases(
        __name__,
        {"ICommandBus": "AbstractCommandBus"},
        globals(),
    )
"""
from __future__ import annotations

import typing as t
import warnings

__all__ = [
    "REMOVED_IN",
    "deprecated_aliases",
    "warn_deprecated",
    "deprecated_callable",
]

#: Versión en la que se eliminan los alias. Se menciona en cada aviso para que el usuario
#: sepa cuánto margen tiene.
REMOVED_IN = "6.0"


def warn_deprecated(
    old: str,
    new: str,
    *,
    kind: str = "nombre",
    stacklevel: int = 3,
) -> None:
    """
    Emite el `DeprecationWarning` estándar de HexCore.

    `stacklevel=3` por defecto para que el warning apunte al código del usuario y no a las
    tripas de HexCore: 1 = esta función, 2 = el `__getattr__` que la llama, 3 = quien pidió
    el nombre.
    """
    # El `kind` va entre corchetes, como etiqueta, para no tener que concordar el artículo
    # ("el nombre" / "la función") con cada categoría.
    warnings.warn(
        f"[{kind}] '{old}' quedó deprecado en HexCore 5.0 y se eliminará en "
        f"{REMOVED_IN}. Usá '{new}' en su lugar.",
        DeprecationWarning,
        stacklevel=stacklevel,
    )


def deprecated_aliases(
    module_name: str,
    aliases: t.Mapping[str, str],
    module_globals: dict[str, t.Any],
) -> t.Callable[[str], t.Any]:
    """
    Construye el `__getattr__` de un módulo que expone alias deprecados.

    Args:
        module_name: `__name__` del módulo, para el mensaje de `AttributeError`.
        aliases: mapa ``{alias_deprecado: nombre_canónico}``. El canónico tiene que estar
            en los globals del módulo.
        module_globals: `globals()` del módulo.

    Returns:
        La función a asignar a `__getattr__`.

    El alias **no** se cachea en los globals: si se cacheara, el segundo acceso no pasaría
    por `__getattr__` y no avisaría. Aquí el coste de resolver es irrelevante y avisar
    siempre es el objetivo.
    """
    def module_getattr(name: str) -> t.Any:
        canonical = aliases.get(name)
        if canonical is None:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")

        warn_deprecated(name, canonical)
        try:
            return module_globals[canonical]
        except KeyError:  # pragma: no cover - error de programación en la librería
            raise AttributeError(
                f"module {module_name!r} declara el alias {name!r} → {canonical!r}, "
                f"pero {canonical!r} no existe en el módulo"
            ) from None

    return module_getattr


def deprecated_callable(
    replacement: t.Callable[..., t.Any],
    old_name: str,
    new_name: str,
    *,
    kind: str = "método",
) -> t.Callable[..., t.Any]:
    """
    Envuelve un callable para que avise al invocarse y delegue en su reemplazo.

    Para métodos deprecados (`EventBus.register`, `reset_sqlalchemy_engine`), donde el
    aviso va en la llamada y no en el acceso al nombre.
    """
    import functools

    @functools.wraps(replacement)
    def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
        warn_deprecated(old_name, new_name, kind=kind, stacklevel=2)
        return replacement(*args, **kwargs)

    wrapper.__name__ = old_name
    wrapper.__doc__ = (
        f"Deprecado desde 5.0, se elimina en {REMOVED_IN}. Usá `{new_name}`.\n\n"
        f"{replacement.__doc__ or ''}"
    )
    return wrapper
