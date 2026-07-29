"""
Resolución de referencias por nombre calificado (FQN).

Un ``__qualname__`` puede contener puntos (clases y funciones anidadas), así que
``fqn.rsplit(".", 1)`` produce un module path inválido: ``"app.Outer.Inner"`` se
parte en ``("app.Outer", "Inner")`` y el import falla con
``No module named 'app.Outer'``.

Este módulo centraliza la resolución correcta: probar prefijos de módulo de más
largo a más corto y luego caminar los atributos restantes.
"""
from __future__ import annotations

import importlib
import typing as t

__all__ = ["resolve_dotted", "build_fqn", "ensure_resolvable_qualname"]


def resolve_dotted(fqn: str) -> t.Any:
    """
    Resuelve un FQN (``"paquete.modulo.Clase.Anidada"``) al objeto que referencia.

    Soporta clases y funciones anidadas: prueba el prefijo de módulo más largo
    que sea importable y después recorre los atributos restantes.

    Raises:
        LookupError: Si no se puede resolver ningún prefijo válido.
    """
    if not fqn or not isinstance(fqn, str):
        raise LookupError(f"FQN inválido: {fqn!r}")

    parts = fqn.split(".")
    if len(parts) == 1:
        # Sin módulo no hay nada que importar; sólo puede ser un builtin.
        import builtins

        try:
            return getattr(builtins, parts[0])
        except AttributeError as exc:
            raise LookupError(f"No se pudo resolver '{fqn}': {exc}") from exc

    last_error: Exception | None = None
    # De más específico a más genérico: "a.b.c" → módulos "a.b.c", "a.b", "a".
    for split_at in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:split_at])
        try:
            obj: t.Any = importlib.import_module(module_path)
        except ImportError as exc:
            last_error = exc
            continue

        try:
            for attr in parts[split_at:]:
                obj = getattr(obj, attr)
        except AttributeError as exc:
            last_error = exc
            continue
        return obj

    raise LookupError(f"No se pudo resolver '{fqn}': {last_error}")


def build_fqn(obj: t.Any) -> str:
    """Construye el FQN de una clase o función (``modulo.QualName``)."""
    qualname = getattr(obj, "__qualname__", None) or obj.__name__
    return f"{obj.__module__}.{qualname}"


def ensure_resolvable_qualname(obj: t.Any, *, decorator: str) -> None:
    """
    Valida que ``obj`` pueda resolverse desde otro proceso.

    Una función definida dentro de otra función lleva ``<locals>`` en su
    ``__qualname__`` y **nunca** será importable desde el worker. Es mucho mejor
    fallar al decorar que fallar en el primer job de producción.

    Raises:
        ValueError: Si el ``__qualname__`` contiene ``<locals>``.
    """
    qualname = getattr(obj, "__qualname__", "") or ""
    if "<locals>" in qualname:
        raise ValueError(
            f"@{decorator} no se puede aplicar a '{qualname}': está definida dentro "
            "de otra función, así que el worker no podrá importarla. Muévela al nivel "
            "de módulo (o a un método de una clase de módulo)."
        )
