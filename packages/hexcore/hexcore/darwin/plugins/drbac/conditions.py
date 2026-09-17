"""
El lenguaje de condiciones de `drbac`: un AST declarativo, sin `eval` ni `getattr` dinámico.

Una política DRBAC necesita expresar cosas como "denegá si el recurso es tuyo" o "permití sólo
en horario hábil", y las dos alternativas obvias son malas:

- **`eval()` de una expresión Python** — la política queda en la base, la escribe quien tiene
  `authz.manage` en un scope (no necesariamente quien puede desplegar código), y `eval` sobre
  ese texto es ejecución de código arbitrario con los privilegios del proceso.
- **Un DSL de texto propio** (parser, gramática) — resuelve la ejecución arbitraria pero es
  trabajo de más, y no se puede compartir "gratis" con el cliente TypeScript (Fase F6): un JSON
  ya es el formato que las dos runtimes entienden sin escribir un segundo parser.

Este módulo es la tercera opción: un árbol pydantic (`Eq`, `And`, `Var`, ...) que serializa a
JSON tal cual, se valida solo al construirse, y se compila a closures Python puras — sin
ningún camino que ejecute una cadena como código, y sin `getattr(obj, nombre_de_afuera)`
dinámico: `Var` sólo puede leer `subject.*`, `resource.*` o `env.*`, resueltos con `dict[clave]`
sobre `Mapping`s de confianza, nunca contra un objeto de dominio.

**Semántica tri-estado (lógica de Kleene, la misma que el `NULL` de SQL).** Una condición no
sólo da `True`/`False`: un `Var` que apunta a un dato que el PIP (Fase F5) todavía no resolvió,
o un predicado no registrado en este proceso, da ``None`` ("indeterminate") — nunca inventa un
valor ni hace `raise`. `And`/`Or`/`Not` propagan ese tercer estado con las mismas reglas que
`NULL AND FALSE = FALSE` pero `NULL AND TRUE = NULL` en SQL: un `False` o un `True` que ya
decide el resultado **domina** sobre cualquier indeterminación de sus hermanos; si nada domina
y hay al menos un indeterminado, el resultado es indeterminado. Quien orquesta la evaluación
completa de una regla (el PDP, Fase F5) es quien decide qué hacer con ese tercer estado —el
plan general es fail-closed: indeterminado se trata como `deny`— pero **ese criterio no vive
acá**: este módulo sólo evalúa la condición tal cual es, sin adivinar hacia ningún lado.

Los límites de tamaño (`ConditionLimits`) se aplican **al construir el compilado**, no sólo
en el camino caliente de evaluar: un AST se guarda una vez y se evalúa muchas, así que frenar
un árbol enorme recién al evaluar ya pagó el costo de haberlo persistido e indexado. El
servicio de políticas (Fase F5) llama `enforce_condition_limits` de nuevo al guardar, antes de
que el árbol llegue a la base.
"""
from __future__ import annotations

import operator
import typing as t
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from hexcore.darwin.domain.exceptions import IdentityError
from hexcore.darwin.plugins.drbac.predicates import PredicateRegistry, default_predicate_registry

__all__ = [
    "ConstScalar",
    "Var",
    "Const",
    "Value",
    "Eq",
    "Ne",
    "Gt",
    "Gte",
    "Lt",
    "Lte",
    "In",
    "Contains",
    "StartsWith",
    "WithinScope",
    "TimeBetween",
    "And",
    "Or",
    "Not",
    "Predicate",
    "Condition",
    "ConditionError",
    "InvalidVarPathError",
    "ConditionTooComplexError",
    "ConditionLimits",
    "DEFAULT_CONDITION_LIMITS",
    "EvaluationContext",
    "MISSING",
    "count_nodes",
    "condition_depth",
    "enforce_condition_limits",
    "compile_condition",
    "evaluate_condition",
    "parse_condition",
    "dump_condition",
]

#: Sentinela sin valor por defecto: distingue "no se pasó este posicional" de "se pasó `None`"
#: a propósito (`Const(None)` es un valor legítimo).
_UNSET: t.Any = object()

#: Los únicos tres namespaces que un `Var` puede leer. Ninguna otra rama del contexto de
#: evaluación es visible para una condición — ni siquiera indirectamente, porque `Var` valida
#: esto al **construirse**, no al evaluar.
_NAMESPACES = ("subject", "resource", "env")

ConstScalar = t.Union[bool, int, float, str, None]


# ── Los operandos: `Var` y `Const` ───────────────────────────────────────────
class _ValueNode(BaseModel):
    """Base común de `Var`/`Const`. No participa de `Condition`: un operando no es una condición
    booleana, es lo que una condición compara."""

    model_config = ConfigDict(frozen=True)


class Var(_ValueNode):
    """
    Una lectura del contexto de evaluación: ``"subject.id"``, ``"resource.owner_id"``,
    ``"env.now"``.

    La ruta se valida **al construirse**, no al evaluar: `subject.*`/`resource.*`/`env.*` son
    los únicos namespaces permitidos, y ninguna otra rama es alcanzable ni siquiera por una
    política que ya está guardada, porque la construcción es el único camino para tener una
    instancia (incluida la que sale de deserializar JSON).

    Uso::

        Var("resource.owner_id")
    """

    type: t.Literal["var"] = "var"
    path: str = Field(min_length=1)

    def __init__(self, path: str | None = None, **data: t.Any) -> None:
        if path is not None:
            data.setdefault("path", path)
        super().__init__(**data)

    @field_validator("path")
    @classmethod
    def _ruta_permitida(cls, valor: str) -> str:
        namespace, separador, resto = valor.partition(".")
        if namespace not in _NAMESPACES or not separador or not resto:
            permitidos = ", ".join(f"{n}.*" for n in _NAMESPACES)
            raise InvalidVarPathError(
                f"'{valor}' no es una ruta válida para Var. Tiene que empezar con uno de "
                f"{permitidos}, seguido de al menos una clave — p.ej. 'resource.owner_id'."
            )
        return valor


class Const(_ValueNode):
    """
    Un literal: ``Const("draft")``, ``Const(5)``, ``Const(["draft", "sent"])``.

    Una lista se guarda como tupla (`ConstScalar` es inmutable) — es la forma que usan `In`/
    `Contains` para el lado de "estos son los valores posibles".
    """

    type: t.Literal["const"] = "const"
    value: ConstScalar | tuple[ConstScalar, ...] = None

    def __init__(self, value: t.Any = _UNSET, **data: t.Any) -> None:
        if value is not _UNSET:
            if isinstance(value, list):
                valor = tuple(t.cast("list[ConstScalar]", value))
            else:
                valor = value
            data.setdefault("value", valor)
        super().__init__(**data)


#: Lo que puede comparar una condición: una lectura del contexto, o un literal. Nunca otra
#: condición booleana — eso es lo que distingue un operando de un nodo de `Condition`.
Value = t.Annotated[t.Union[Var, Const], Field(discriminator="type")]


def _as_value(candidate: t.Any) -> t.Any:
    """
    Envuelve un literal Python en un `Const`, para que ``Eq(Var("resource.status"), "draft")``
    no obligue a escribir ``Const("draft")`` a mano.

    Un `Var`/`Const` ya construidos, o un dict que ya tiene forma de uno (deserializado desde
    JSON: ``{"type": "var", ...}``), se dejan pasar tal cual — es la validación de pydantic la
    que los termina de resolver contra `Value`. Envolverlos acá igual sería incorrecto: un dict
    `{"type": "var", "path": "resource.id"}` pasado en crudo no es el literal Python `{"type":
    "var", "path": "resource.id"}`, es un `Var` ya serializado.
    """
    if isinstance(candidate, (Var, Const)):
        return candidate
    if isinstance(candidate, dict):
        crudo = t.cast("dict[str, t.Any]", candidate)
        if crudo.get("type") in ("var", "const"):
            return crudo
    if isinstance(candidate, (list, tuple)):
        return Const(value=list(t.cast("t.Iterable[ConstScalar]", candidate)))
    return Const(value=candidate)


# ── Las condiciones binarias sobre dos `Value` ───────────────────────────────
class _BinaryValueCondition(BaseModel):
    """
    Base de toda condición con dos operandos `Value`. El significado de `left`/`right` lo
    documenta cada subclase — no siempre es simétrico (`In`, `Contains`, `WithinScope` leen
    cada lado con un rol distinto).
    """

    model_config = ConfigDict(frozen=True)

    left: Value
    right: Value

    def __init__(self, left: t.Any = _UNSET, right: t.Any = _UNSET, **data: t.Any) -> None:
        if left is not _UNSET:
            data.setdefault("left", left)
        if right is not _UNSET:
            data.setdefault("right", right)
        super().__init__(**data)

    @field_validator("left", "right", mode="before")
    @classmethod
    def _coerce(cls, valor: t.Any) -> t.Any:
        return _as_value(valor)


class Eq(_BinaryValueCondition):
    """``left == right``."""

    type: t.Literal["eq"] = "eq"


class Ne(_BinaryValueCondition):
    """``left != right``."""

    type: t.Literal["ne"] = "ne"


class Gt(_BinaryValueCondition):
    """``left > right``. Indeterminado si los tipos no son comparables entre sí."""

    type: t.Literal["gt"] = "gt"


class Gte(_BinaryValueCondition):
    """``left >= right``."""

    type: t.Literal["gte"] = "gte"


class Lt(_BinaryValueCondition):
    """``left < right``."""

    type: t.Literal["lt"] = "lt"


class Lte(_BinaryValueCondition):
    """``left <= right``."""

    type: t.Literal["lte"] = "lte"


class In(_BinaryValueCondition):
    """``left`` está entre los valores de ``right`` (que suele resolver a una lista de `Const`)."""

    type: t.Literal["in"] = "in"


class Contains(_BinaryValueCondition):
    """``right`` está adentro de ``left`` (una lista o un string) — el espejo de `In`."""

    type: t.Literal["contains"] = "contains"


class StartsWith(_BinaryValueCondition):
    """``left`` (un string) empieza con ``right`` (un string)."""

    type: t.Literal["starts_with"] = "starts_with"


class WithinScope(_BinaryValueCondition):
    """
    ``left`` (un `scope_path` como ``"org:42/project:7"``) está en o debajo de ``right`` (el
    ancestro, ``"org:42"``).

    La comparación es **por segmentos** separados por ``/``, nunca `str.startswith` crudo:
    ``"org:4"`` no puede ser ancestro de ``"org:42/..."`` sólo porque el string es un prefijo
    —ésa es exactamente la escalada cross-tenant que el plan de rbac/drbac marca como riesgo—,
    tiene que coincidir segmento por segmento.
    """

    type: t.Literal["within_scope"] = "within_scope"


# ── Las condiciones ternarias/n-arias ────────────────────────────────────────
class TimeBetween(BaseModel):
    """``start <= value <= end``, con los tres operandos resueltos a `datetime`."""

    model_config = ConfigDict(frozen=True)

    type: t.Literal["time_between"] = "time_between"
    value: Value
    start: Value
    end: Value

    def __init__(
        self,
        value: t.Any = _UNSET,
        start: t.Any = _UNSET,
        end: t.Any = _UNSET,
        **data: t.Any,
    ) -> None:
        if value is not _UNSET:
            data.setdefault("value", value)
        if start is not _UNSET:
            data.setdefault("start", start)
        if end is not _UNSET:
            data.setdefault("end", end)
        super().__init__(**data)

    @field_validator("value", "start", "end", mode="before")
    @classmethod
    def _coerce(cls, valor: t.Any) -> t.Any:
        return _as_value(valor)


class And(BaseModel):
    """Todas las condiciones tienen que dar `True` — lógica de Kleene, ver el docstring del módulo."""

    model_config = ConfigDict(frozen=True)

    type: t.Literal["and"] = "and"
    items: tuple["Condition", ...] = Field(min_length=1)

    def __init__(self, *items: t.Any, **data: t.Any) -> None:
        if items:
            data.setdefault("items", tuple(items))
        super().__init__(**data)


class Or(BaseModel):
    """Alguna condición tiene que dar `True` — lógica de Kleene, ver el docstring del módulo."""

    model_config = ConfigDict(frozen=True)

    type: t.Literal["or"] = "or"
    items: tuple["Condition", ...] = Field(min_length=1)

    def __init__(self, *items: t.Any, **data: t.Any) -> None:
        if items:
            data.setdefault("items", tuple(items))
        super().__init__(**data)


class Not(BaseModel):
    """Invierte el resultado. Un indeterminado sigue indeterminado: `not None` no es `True`."""

    model_config = ConfigDict(frozen=True)

    type: t.Literal["not"] = "not"
    item: "Condition"

    def __init__(self, item: t.Any = _UNSET, **data: t.Any) -> None:
        if item is not _UNSET:
            data.setdefault("item", item)
        super().__init__(**data)


class Predicate(BaseModel):
    """
    Un predicado con nombre, para lo que este AST no cubre — ver `predicates.py`.

    Sólo se evalúa del lado del servidor: una regla que use `Predicate` en su condición no es
    ``client_evaluable`` (Fase F5), porque el cliente no tiene forma de saber qué hace el
    predicado sin ejecutar código Python arbitrario, que es justo lo que este módulo evita.

    Uso::

        Predicate("business_hours")
        Predicate("min_amount", Var("resource.amount"), Const(1000))
    """

    model_config = ConfigDict(frozen=True)

    type: t.Literal["predicate"] = "predicate"
    name: str = Field(min_length=1)
    args: tuple[Value, ...] = ()

    def __init__(self, name: str | None = None, *args: t.Any, **data: t.Any) -> None:
        if name is not None:
            data.setdefault("name", name)
        if args:
            data.setdefault("args", tuple(args))
        super().__init__(**data)

    @field_validator("args", mode="before")
    @classmethod
    def _coerce_args(cls, valor: t.Any) -> t.Any:
        if isinstance(valor, (list, tuple)):
            crudos = t.cast("t.Iterable[t.Any]", valor)
            return tuple(_as_value(a) for a in crudos)
        return valor


#: El árbol completo. `Var`/`Const` no están acá a propósito: son operandos, no condiciones —
#: `Condition` siempre resuelve a `True`/`False`/indeterminado, nunca a un valor suelto.
Condition = t.Annotated[
    t.Union[
        Eq,
        Ne,
        Gt,
        Gte,
        Lt,
        Lte,
        In,
        Contains,
        StartsWith,
        WithinScope,
        TimeBetween,
        And,
        Or,
        Not,
        Predicate,
    ],
    Field(discriminator="type"),
]

# `And`/`Or`/`Not` se refieren a `Condition` antes de que exista en el módulo (con `from
# __future__ import annotations` la anotación queda como string hasta acá). `model_rebuild()`
# resuelve la referencia diferida una sola vez, al importar el módulo.
And.model_rebuild()
Or.model_rebuild()
Not.model_rebuild()

#: Para parsear/serializar un `Condition` suelto (no es un `BaseModel` propio, es la unión).
_CONDITION_ADAPTER: TypeAdapter[Condition] = TypeAdapter(Condition)


def parse_condition(data: t.Mapping[str, t.Any]) -> Condition:
    """Un dict (típicamente ya deserializado de la columna `condition JSON`) a `Condition`."""
    return _CONDITION_ADAPTER.validate_python(data)


def dump_condition(node: Condition) -> dict[str, t.Any]:
    """El dict JSON-safe de `node`, para guardar en la columna `condition JSON`."""
    return _CONDITION_ADAPTER.dump_python(node, mode="json")


# ── Las excepciones ───────────────────────────────────────────────────────────
class ConditionError(IdentityError):
    """Base de las fallas de este módulo."""


class InvalidVarPathError(ConditionError):
    """La ruta de un `Var` no está en el allowlist `subject.*`/`resource.*`/`env.*`."""


class ConditionTooComplexError(ConditionError):
    """La condición excede `ConditionLimits.max_nodes` o `.max_depth`. Ver `enforce_condition_limits`."""


# ── Los límites de tamaño ─────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ConditionLimits:
    """
    Cuánto puede crecer un árbol de condición antes de que se rechace.

    Los valores por defecto son deliberadamente chicos: una condición legítima —"es tu propio
    recurso", "estás en horario hábil y en tu scope"— tiene un puñado de nodos. Un árbol de
    cientos de nodos o decenas de niveles no es una política, es un intento de agotar CPU en
    cada evaluación (ver la fila "Política maliciosa / DoS" del plan de rbac/drbac).
    """

    max_nodes: int = 256
    max_depth: int = 16


DEFAULT_CONDITION_LIMITS = ConditionLimits()


def _hijos(node: Condition) -> tuple[Condition, ...]:
    """
    Los nodos de `Condition` directamente anidados en `node`.

    Los `Value` (`Var`/`Const`) de `Predicate.args` no cuentan: no son condiciones anidadas, y
    un `Const` con una lista larga es un problema de tamaño de dato, no de profundidad de
    árbol — fuera del alcance de este límite.
    """
    if isinstance(node, (And, Or)):
        return node.items
    if isinstance(node, Not):
        return (node.item,)
    return ()


def count_nodes(node: Condition) -> int:
    """Cuántos nodos de `Condition` tiene el árbol, contando `node`."""
    return 1 + sum(count_nodes(hijo) for hijo in _hijos(node))


def condition_depth(node: Condition) -> int:
    """Cuántos niveles de anidamiento tiene el árbol. Una hoja (`Eq`, `Predicate`, ...) es 1."""
    hijos = _hijos(node)
    if not hijos:
        return 1
    return 1 + max(condition_depth(hijo) for hijo in hijos)


def enforce_condition_limits(
    node: Condition, limits: ConditionLimits = DEFAULT_CONDITION_LIMITS
) -> None:
    """
    Valida el tamaño de `node` **antes de guardar** una política (Fase F5), no sólo antes de
    evaluar — ver el docstring del módulo.

    Raises:
        ConditionTooComplexError
    """
    nodos = count_nodes(node)
    if nodos > limits.max_nodes:
        raise ConditionTooComplexError(
            f"La condición tiene {nodos} nodos, más que el límite de {limits.max_nodes}."
        )
    profundidad = condition_depth(node)
    if profundidad > limits.max_depth:
        raise ConditionTooComplexError(
            f"La condición tiene {profundidad} niveles de anidamiento, más que el límite de "
            f"{limits.max_depth}."
        )


# ── El contexto de evaluación ─────────────────────────────────────────────────
class EvaluationContext(BaseModel):
    """
    Los tres namespaces que un `Var` puede leer. Siempre `Mapping`s planos —nunca un objeto de
    dominio— porque la resolución no usa `getattr`: ver `_namespace_de` más abajo.

    `resource`/`env` llegan ya resueltos: completar `resource.*` con datos que no vinieron en
    el `ResourceRef` original es trabajo del PIP (Fase F5), no de este módulo.
    """

    model_config = ConfigDict(frozen=True)

    subject: t.Mapping[str, t.Any] = Field(default_factory=dict)
    resource: t.Mapping[str, t.Any] = Field(default_factory=dict)
    env: t.Mapping[str, t.Any] = Field(default_factory=dict)


class _Missing:
    """
    Sentinela: un `Var` no se pudo resolver (falta la clave, o algún nivel intermedio no es un
    `Mapping`). Deliberadamente **no** es `None`: un atributo de recurso puede legítimamente
    valer `None`, y confundir "no está" con "vale None" convertiría un dato ausente en un
    `Eq(Var(...), Const(None))` que da `True` por accidente.
    """

    def __repr__(self) -> str:
        return "<missing>"


MISSING: t.Final = _Missing()

#: `True`/`False` deciden; `None` es "indeterminate" — ver la sección de tri-estado del
#: docstring del módulo.
ConditionResult = t.Optional[bool]

ValueEvaluator = t.Callable[[EvaluationContext], t.Any]
ConditionEvaluator = t.Callable[[EvaluationContext], ConditionResult]


def _namespace_de(context: EvaluationContext, namespace: str) -> t.Mapping[str, t.Any]:
    """
    El `Mapping` de `namespace`, con un `if/elif` explícito y **no** `getattr(context,
    namespace)`: `Var.path` ya está validado contra un allowlist cerrado de 3 nombres, pero
    este módulo evita además cualquier lectura de atributo dirigida por un string que no sea
    un literal en el propio código — es la otra mitad de "sin `eval`/`getattr` dinámico".
    """
    if namespace == "subject":
        return context.subject
    if namespace == "resource":
        return context.resource
    if namespace == "env":
        return context.env
    raise AssertionError(f"namespace fuera del allowlist: '{namespace}' (bug en Var._ruta_permitida)")


def _compile_value(node: Value) -> ValueEvaluator:
    if isinstance(node, Const):
        valor = node.value
        return lambda _context: valor

    # `Value` es `Var | Const`; el `Const` ya devolvió arriba, así que acá sólo puede ser `Var`
    # — pyright ya lo sabe por la propia unión, de ahí que no haga falta otro `isinstance`.
    namespace, _, resto = node.path.partition(".")
    claves = resto.split(".")

    def _leer(context: EvaluationContext) -> t.Any:
        actual: t.Any = _namespace_de(context, namespace)
        for clave in claves:
            if not isinstance(actual, t.Mapping):
                return MISSING
            mapa = t.cast("t.Mapping[str, t.Any]", actual)
            if clave not in mapa:
                return MISSING
            actual = mapa[clave]
        return actual

    return _leer


def _as_datetime(valor: t.Any) -> datetime | None:
    if isinstance(valor, datetime):
        return valor
    if isinstance(valor, str):
        try:
            return datetime.fromisoformat(valor)
        except ValueError:
            return None
    return None


def _compile_comparison(
    node: _BinaryValueCondition, operador: t.Callable[[t.Any, t.Any], bool]
) -> ConditionEvaluator:
    izquierda = _compile_value(node.left)
    derecha = _compile_value(node.right)

    def _evaluar(context: EvaluationContext) -> ConditionResult:
        valor_izq, valor_der = izquierda(context), derecha(context)
        if valor_izq is MISSING or valor_der is MISSING:
            return None
        try:
            return bool(operador(valor_izq, valor_der))
        except TypeError:
            # Tipos no comparables entre sí (un string contra un número, por ejemplo): no es
            # un bug de la política, es una condición que no aplica a este dato — indeterminado,
            # no una excepción que tumbe la evaluación de toda la regla.
            return None

    return _evaluar


def _compile_membership(node: _BinaryValueCondition, *, invertido: bool) -> ConditionEvaluator:
    """`In` (`invertido=False`): `left in right`. `Contains` (`invertido=True`): `right in left`."""
    izquierda = _compile_value(node.left)
    derecha = _compile_value(node.right)

    def _evaluar(context: EvaluationContext) -> ConditionResult:
        valor_izq, valor_der = izquierda(context), derecha(context)
        if valor_izq is MISSING or valor_der is MISSING:
            return None
        contenedor, elemento = (valor_izq, valor_der) if invertido else (valor_der, valor_izq)
        try:
            return elemento in contenedor
        except TypeError:
            return None

    return _evaluar


def _compile_starts_with(node: StartsWith) -> ConditionEvaluator:
    izquierda = _compile_value(node.left)
    derecha = _compile_value(node.right)

    def _evaluar(context: EvaluationContext) -> ConditionResult:
        valor, prefijo = izquierda(context), derecha(context)
        if valor is MISSING or prefijo is MISSING:
            return None
        if not isinstance(valor, str) or not isinstance(prefijo, str):
            return None
        return valor.startswith(prefijo)

    return _evaluar


def _dentro_de_scope(path: str, ancestro: str) -> bool:
    if ancestro == "":
        return True  # El scope global es ancestro de cualquier cosa.
    segmentos_path = path.split("/") if path else []
    segmentos_ancestro = ancestro.split("/") if ancestro else []
    if len(segmentos_ancestro) > len(segmentos_path):
        return False
    # Por segmentos, no `str.startswith` — ver el docstring de `WithinScope`.
    return segmentos_path[: len(segmentos_ancestro)] == segmentos_ancestro


def _compile_within_scope(node: WithinScope) -> ConditionEvaluator:
    izquierda = _compile_value(node.left)
    derecha = _compile_value(node.right)

    def _evaluar(context: EvaluationContext) -> ConditionResult:
        path, ancestro = izquierda(context), derecha(context)
        if path is MISSING or ancestro is MISSING:
            return None
        if not isinstance(path, str) or not isinstance(ancestro, str):
            return None
        return _dentro_de_scope(path, ancestro)

    return _evaluar


def _compile_time_between(node: TimeBetween) -> ConditionEvaluator:
    valor_ev = _compile_value(node.value)
    inicio_ev = _compile_value(node.start)
    fin_ev = _compile_value(node.end)

    def _evaluar(context: EvaluationContext) -> ConditionResult:
        crudo_valor, crudo_inicio, crudo_fin = (
            valor_ev(context),
            inicio_ev(context),
            fin_ev(context),
        )
        if MISSING in (crudo_valor, crudo_inicio, crudo_fin):
            return None
        valor, inicio, fin = (
            _as_datetime(crudo_valor),
            _as_datetime(crudo_inicio),
            _as_datetime(crudo_fin),
        )
        if valor is None or inicio is None or fin is None:
            return None
        try:
            return inicio <= valor <= fin
        except TypeError:
            # Mezcla de datetime naive y aware, por ejemplo: no comparables entre sí.
            return None

    return _evaluar


def _and(compilados: t.Sequence[ConditionEvaluator]) -> ConditionEvaluator:
    def _evaluar(context: EvaluationContext) -> ConditionResult:
        vio_indeterminado = False
        for hijo in compilados:
            resultado = hijo(context)
            if resultado is False:
                return False  # False domina: no hace falta seguir evaluando.
            if resultado is None:
                vio_indeterminado = True
        return None if vio_indeterminado else True

    return _evaluar


def _or(compilados: t.Sequence[ConditionEvaluator]) -> ConditionEvaluator:
    def _evaluar(context: EvaluationContext) -> ConditionResult:
        vio_indeterminado = False
        for hijo in compilados:
            resultado = hijo(context)
            if resultado is True:
                return True  # True domina.
            if resultado is None:
                vio_indeterminado = True
        return None if vio_indeterminado else False

    return _evaluar


def _not(compilado: ConditionEvaluator) -> ConditionEvaluator:
    def _evaluar(context: EvaluationContext) -> ConditionResult:
        resultado = compilado(context)
        return None if resultado is None else not resultado

    return _evaluar


def _compile_predicate(node: Predicate, registry: PredicateRegistry) -> ConditionEvaluator:
    argumentos = tuple(_compile_value(a) for a in node.args)

    def _evaluar(context: EvaluationContext) -> ConditionResult:
        funcion = registry.get(node.name)
        if funcion is None:
            # Una política vieja de un módulo que ya no registra este predicado, o un typo al
            # guardar: indeterminado, nunca un error que tumbe la evaluación de la regla.
            return None
        valores = [a(context) for a in argumentos]
        if any(v is MISSING for v in valores):
            return None
        try:
            resultado = funcion(context, *valores)
        except Exception:
            # Un predicado de terceros puede fallar por su cuenta (una API externa caída, por
            # ejemplo): se trata igual que un provider que explota en `AuthorizationEngine`,
            # nunca como un `allow` ni como una excepción que rompa el request.
            return None
        return None if resultado is None else bool(resultado)

    return _evaluar


_COMPARATORS: dict[type, t.Callable[[t.Any, t.Any], bool]] = {
    Eq: operator.eq,
    Ne: operator.ne,
    Gt: operator.gt,
    Gte: operator.ge,
    Lt: operator.lt,
    Lte: operator.le,
}


def _compile(node: Condition, registry: PredicateRegistry) -> ConditionEvaluator:
    operador = _COMPARATORS.get(type(node))
    if operador is not None:
        assert isinstance(node, _BinaryValueCondition)  # narrowing: type(node) está en el dict
        return _compile_comparison(node, operador)

    if isinstance(node, In):
        return _compile_membership(node, invertido=False)
    if isinstance(node, Contains):
        return _compile_membership(node, invertido=True)
    if isinstance(node, StartsWith):
        return _compile_starts_with(node)
    if isinstance(node, WithinScope):
        return _compile_within_scope(node)
    if isinstance(node, TimeBetween):
        return _compile_time_between(node)
    if isinstance(node, And):
        return _and(tuple(_compile(hijo, registry) for hijo in node.items))
    if isinstance(node, Or):
        return _or(tuple(_compile(hijo, registry) for hijo in node.items))
    if isinstance(node, Not):
        return _not(_compile(node.item, registry))
    if isinstance(node, Predicate):
        return _compile_predicate(node, registry)

    raise AssertionError(f"tipo de Condition no manejado: {node!r}")


def compile_condition(
    node: Condition,
    *,
    predicates: PredicateRegistry | None = None,
    limits: ConditionLimits = DEFAULT_CONDITION_LIMITS,
) -> ConditionEvaluator:
    """
    Compila `node` a una función pura `EvaluationContext -> True|False|None`.

    Para el camino caliente del PDP (Fase F5): compilá una vez por política/versión y
    reutilizá el resultado en cada evaluación, en vez de recorrer el árbol de nuevo por
    request. `evaluate_condition` es el atajo para cuando no hace falta ese cacheo.

    `limits` se revisa acá también —no sólo al guardar la política— como defensa en
    profundidad: ver el docstring del módulo.

    Raises:
        ConditionTooComplexError: si `node` excede `limits`.
    """
    enforce_condition_limits(node, limits)
    registro = predicates if predicates is not None else default_predicate_registry()
    return _compile(node, registro)


def evaluate_condition(
    node: Condition,
    context: EvaluationContext,
    *,
    predicates: PredicateRegistry | None = None,
    limits: ConditionLimits = DEFAULT_CONDITION_LIMITS,
) -> ConditionResult:
    """Compila y evalúa en un solo paso. Para tests, o para una evaluación de una sola vez."""
    return compile_condition(node, predicates=predicates, limits=limits)(context)
