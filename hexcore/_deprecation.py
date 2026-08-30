"""
Mecanismo de deprecación de HexCore.

Los alias anteriores a 5.0 (`ICommandBus`, `ISerializer`, `NoSqlUnitOfWork`, …) **se
eliminaron en 7.0**: estaban deprecados desde 5.0, o sea dos majors de aviso. Este módulo
queda como el mecanismo, no como su inventario — lo usa lo que se deprece de acá en adelante,
empezando por `hexcore.domain.auth`, que Darwin reemplaza.

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
    "deprecated_lazy_names",
    "warn_deprecated",
    "deprecated_callable",
]

#: Versión en la que se elimina lo que se deprece ahora. Se menciona en cada aviso para que
#: el usuario sepa cuánto margen tiene.
#:
#: **Tiene que ser estrictamente mayor que la versión publicada**, o el aviso se contradice a
#: sí mismo: anunciar "se elimina en 6.0" corriendo en 6.0.0, con los alias delante, le enseña
#: al usuario que estos avisos no hay que leerlos. Ya pasó, y no por descuido sino porque en
#: este repo dos majors salieron de commits `feat!:` involuntarios.
#:
#: En 7.0 los alias pre-5.0 **se eliminaron de verdad**. La constante apunta siempre al
#: próximo major por publicar: hoy 9.0, porque 8.0.0 es la serie que se está por sacar.
#: No hay nada deprecado esperando esa fecha — la constante es la ventana disponible para
#: lo que se deprece de acá en adelante, y por eso se corre con cada major en vez de
#: quedarse fija.
#:
#: Lo vigila `test_removed_in_is_ahead_of_the_published_version`: si un bump vuelve a alcanzar
#: este valor, el fallo salta en CI y no en el aviso que lee el usuario. Ese test es el que
#: convierte "se nos pasó" en "no se puede releasear".
REMOVED_IN = "9.0"


def warn_deprecated(
    old: str,
    new: str,
    *,
    kind: str = "nombre",
    since: str = "5.0",
    stacklevel: int = 3,
) -> None:
    """
    Emite el `DeprecationWarning` estándar de HexCore.

    `stacklevel=3` por defecto para que el warning apunte al código del usuario y no a las
    tripas de HexCore: 1 = esta función, 2 = el `__getattr__` que la llama, 3 = quien pidió
    el nombre.

    `since` es la versión en la que el nombre quedó deprecado, y es un parámetro porque no todo
    se deprecó a la vez: los alias pre-5.0 decían "5.0", y lo que se deprece de acá en adelante
    dice su propia versión. Hardcodearlo hacía que un aviso nuevo mintiera sobre cuándo empezó
    el margen — y el margen es la única información accionable del mensaje.
    """
    # El `kind` va entre corchetes, como etiqueta, para no tener que concordar el artículo
    # ("el nombre" / "la función") con cada categoría.
    warnings.warn(
        f"[{kind}] '{old}' quedó deprecado en HexCore {since} y se eliminará en "
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


def deprecated_lazy_names(
    module_name: str,
    replacements: t.Mapping[str, str],
    loaders: t.Mapping[str, t.Callable[[], t.Any]],
    *,
    since: str = "7.0",
) -> t.Callable[[str], t.Any]:
    """
    Construye un `__getattr__` que avisa y devuelve **el objeto viejo**, cargado perezosamente.

    La diferencia con `deprecated_aliases`, que es la razón de que existan las dos: `deprecated_aliases`
    resuelve el alias al **reemplazo** —sirve cuando los dos nombres apuntan a lo mismo— y esto
    devuelve lo viejo, que sigue existiendo con su propia forma. Es el caso de
    `hexcore.domain.auth`: `TokenClaims` y `AccessTokenClaims` **no** son intercambiables (distintos
    campos, distintos invariantes), así que aliasarlos rompería a quien todavía use el viejo. Lo que
    hace falta es que siga funcionando **y avise**.

    Y perezoso porque el objeto viejo puede vivir en un módulo que no se quiere importar en el
    arranque: el `from` eager es justamente lo que se está sacando.

    Args:
        module_name: `__name__` del módulo, para el mensaje de `AttributeError`.
        replacements: mapa ``{nombre_deprecado: nombre_del_reemplazo}``. El reemplazo se nombra en
            el aviso; no hace falta que sea importable desde acá.
        loaders: mapa ``{nombre_deprecado: función_que_lo_devuelve}``. Se invoca en cada acceso.
        since: La versión en la que se deprecaron. `"7.0"` por default, que es cuando se
            introdujo este mecanismo; los alias pre-5.0 usan `deprecated_aliases`.

    El resultado **no se cachea** en los globals: si se cacheara, el segundo acceso no pasaría por
    `__getattr__` y no avisaría. Avisar siempre es el objetivo, y el coste de resolver es
    irrelevante frente a eso.

    Uso::

        from hexcore._deprecation import deprecated_lazy_names

        __getattr__ = deprecated_lazy_names(
            __name__,
            {"TokenClaims": "hexcore.darwin.AccessTokenClaims"},
            {"TokenClaims": lambda: _cargar_token_claims()},
        )
    """
    def module_getattr(name: str) -> t.Any:
        reemplazo = replacements.get(name)
        cargar = loaders.get(name)
        if reemplazo is None or cargar is None:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")

        warn_deprecated(name, reemplazo, since=since)
        return cargar()

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

    Para métodos deprecados, donde el aviso va en la llamada y no en el acceso al nombre.
    Sus dos usuarios originales —`EventBus.register` y `reset_sqlalchemy_engine`— se
    eliminaron en 7.0; el helper queda para el próximo.
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
