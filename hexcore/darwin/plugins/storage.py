"""
Cómo un plugin resuelve su backend de almacenamiento.

Un plugin con tabla propia tiene el mismo problema que el núcleo: sus repositorios están
implementados sobre un backend, y no puede importarlo en el nivel superior porque eso haría que
nombrar el plugin exija el extra. La diferencia es que un plugin **no tiene contenedor propio**, así
que necesita una función que le resuelva el módulo.

Es una sola función, y su valor está en lo que hace en el camino de error: un plugin que pide su
repositorio en un despliegue con el backend equivocado tiene que enterarse con un mensaje que diga
qué instalar, no con un `ModuleNotFoundError` sobre un submódulo que el consumidor nunca nombró.

Uso, en un plugin::

    from hexcore.darwin.plugins.storage import plugin_repositories

    def _repositorio_por_defecto():
        return plugin_repositories("two_factor").TwoFactorRepository()
"""
from __future__ import annotations

import typing as t

__all__ = ["plugin_repositories", "plugin_storage_backend"]


def plugin_storage_backend() -> str:
    """
    El backend que resolvió el contenedor de identidad.

    Se le pregunta **al contenedor** y no se resuelve de nuevo: si un plugin detectara por su
    cuenta, un despliegue con los dos extras instalados podría terminar con el núcleo en un backend
    y un plugin en el otro — y el síntoma es que el login funciona y el segundo factor no encuentra
    nada.
    """
    from hexcore.darwin.application.container import get_identity_container

    return get_identity_container().storage_backend


def plugin_repositories(plugin: str) -> t.Any:
    """
    El módulo de repositorios del plugin, para el backend resuelto.

    Args:
        plugin: El nombre del paquete del plugin (`"two_factor"`, `"oauth"`, …).

    Returns:
        El módulo `hexcore.darwin.plugins.{plugin}.orms.{backend}.repository`.

    Raises:
        ImportError: el plugin no implementó ese backend, con la lista de los que sí. Es el caso
            real: un plugin de terceros puede shippear sólo SQL, y quien lo cablea con Mongo tiene
            que enterarse al arrancar y no en el primer request.
    """
    import importlib

    backend = plugin_storage_backend()
    ruta = f"hexcore.darwin.plugins.{plugin}.orms.{backend}.repository"

    try:
        return importlib.import_module(ruta)
    except ModuleNotFoundError as exc:
        # Se distingue "el plugin no tiene ese backend" de "falta el paquete de tercero": el
        # primero es un plugin incompleto y el segundo es un extra sin instalar, y la remediación
        # de cada uno es distinta.
        if exc.name is not None and exc.name.startswith("hexcore."):
            disponibles = _backends_del_plugin(plugin)
            raise ImportError(
                f"El plugin {plugin!r} no implementa el backend {backend!r}.\n\n"
                f"Implementa: {', '.join(disponibles) or '(ninguno)'}.\n\n"
                f"O cambiá el backend de Darwin, o pasale al plugin un repositorio propio:\n\n"
                f"    {plugin.title().replace('_', '')}Plugin(repository=MiRepositorio())"
            ) from exc
        raise


def _backends_del_plugin(plugin: str) -> list[str]:
    """
    Qué backends implementa el plugin, mirando el paquete `orms/`.

    Se lee del sistema de archivos y no de una lista declarada: una lista se desactualiza en
    silencio cuando alguien agrega un backend, y el único uso de esto es un mensaje de error —
    donde una lista incompleta es peor que ninguna.
    """
    import importlib
    import pkgutil

    try:
        paquete = importlib.import_module(f"hexcore.darwin.plugins.{plugin}.orms")
    except ImportError:  # pragma: no cover - el plugin no tiene almacenamiento
        return []

    rutas = getattr(paquete, "__path__", None)
    if rutas is None:  # pragma: no cover - no es un paquete
        return []

    return sorted(m.name for m in pkgutil.iter_modules(list(rutas)) if m.ispkg)
