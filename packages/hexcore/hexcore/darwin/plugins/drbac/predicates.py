"""
`PredicateRegistry`: predicados con nombre para lo que el AST declarativo de `conditions.py`
no cubre.

El AST (`Eq`, `And`, `WithinScope`, ...) es deliberadamente chico: cubre comparaciones y
combinadores, no "es horario hábil en el huso horario de la organización" ni "el monto está
por debajo del límite de aprobación de este rol en este mes". Agregar un nodo nuevo al AST por
cada regla de negocio que aparezca convertiría el lenguaje declarativo en un lenguaje de
programación disfrazado — que es justo lo que `conditions.py` evita.

`Predicate("business_hours")` en una condición no ejecuta código arbitrario: resuelve, por
nombre, a una función que alguien registró **explícitamente** en este proceso con
`@registry.predicate("business_hours")`. Si el nombre no está registrado —una política vieja
de un módulo que se desinstaló, un typo al declarar la política— la evaluación es
indeterminada (ver `conditions._compile_predicate`), nunca un error que tumbe el request ni un
`allow` por default.

Un predicado se evalúa **siempre en el servidor**: una regla cuya condición use `Predicate` no
es ``client_evaluable`` (Fase F5), porque el cliente no tiene forma de saber qué hace sin
correr el mismo código Python.
"""
from __future__ import annotations

import threading
import typing as t

__all__ = [
    "PredicateFn",
    "PredicateRegistry",
    "default_predicate_registry",
    "reset_default_predicate_registry",
]

if t.TYPE_CHECKING:
    from hexcore.darwin.plugins.drbac.conditions import EvaluationContext

class PredicateFn(t.Protocol):
    """
    `(context, *argumentos_ya_resueltos) -> True | False | None`. Los argumentos que declara
    `Predicate.args` llegan **ya evaluados** —no como `Value`—, así que un predicado nunca
    necesita saber nada de `conditions.py` para leerlos. `None` es "indeterminado", igual que
    el resto del módulo; una excepción del predicado también se trata como indeterminado (la
    atrapa quien compila, no el predicado).
    """

    def __call__(self, context: "EvaluationContext", *args: t.Any) -> bool | None: ...


class PredicateRegistry:
    """
    Registro de predicados, por nombre. Instanciable, no un singleton global — mismo criterio
    que `RoleRegistry`: los tests necesitan registros aislados, y `default_predicate_registry()`
    es el atajo para quien no quiere pasar uno a mano por todas las capas.

    Thread-safe con `RLock`, porque es estado mutable compartido igual que `RoleRegistry` y
    `HandlerRegistry`.

    Uso::

        registro = PredicateRegistry()

        @registro.predicate("business_hours")
        def _horario_habil(context: EvaluationContext) -> bool:
            hora = context.env.get("now")
            return hora is not None and 9 <= hora.hour < 18

        @registro.predicate("min_amount")
        def _monto_minimo(context: EvaluationContext, umbral: float) -> bool:
            monto = context.resource.get("amount")
            return monto is not None and monto >= umbral
    """

    def __init__(self) -> None:
        self._funciones: dict[str, PredicateFn] = {}
        self._lock = threading.RLock()

    def predicate(self, name: str) -> t.Callable[[PredicateFn], PredicateFn]:
        """
        Decorador: registra `fn` como el predicado `name`.

        Raises:
            ValueError: si `name` ya está registrado. Igual que `RoleRegistry.register_role`,
                una redefinición silenciosa dependería del orden de importación de módulos.
        """

        def _decorador(fn: PredicateFn) -> PredicateFn:
            with self._lock:
                if name in self._funciones:
                    raise ValueError(
                        f"Ya hay un predicado registrado como '{name}'. Si es a propósito, "
                        f"llamá a `registry.unregister('{name}')` primero."
                    )
                self._funciones[name] = fn
            return fn

        return _decorador

    def unregister(self, name: str) -> None:
        """Da de baja `name`, si estaba. No lanza si no estaba — para limpiar tests sin ceremonia."""
        with self._lock:
            self._funciones.pop(name, None)

    def get(self, name: str) -> PredicateFn | None:
        """La función registrada como `name`, o `None` si no hay ninguna."""
        with self._lock:
            return self._funciones.get(name)

    def names(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._funciones)

    def clear(self) -> None:
        """Vacía el registro. Para aislar tests."""
        with self._lock:
            self._funciones.clear()


# ── Registro por defecto del proceso ─────────────────────────────────────────
_default: PredicateRegistry | None = None
_default_lock = threading.RLock()


def default_predicate_registry() -> PredicateRegistry:
    """El registro compartido del proceso, creado al primer uso — mismo patrón que `domain.permissions.default_registry`."""
    global _default
    with _default_lock:
        if _default is None:
            _default = PredicateRegistry()
        return _default


def reset_default_predicate_registry() -> None:
    """Descarta el registro compartido. Para los tests."""
    global _default
    with _default_lock:
        _default = None
