"""
Darwin — Fase F4 del plan rbac/drbac: el lenguaje de condiciones de `drbac`.

Lo que se fija:

1. `Var` sólo puede leer `subject.*`/`resource.*`/`env.*`, validado al **construirse**.
2. La semántica tri-estado: un `Var` que no resuelve es indeterminado (`None`), y `And`/`Or`/
   `Not` lo propagan con lógica de Kleene — el mismo `NULL` de SQL, no "trata indeterminado
   como falso".
3. `WithinScope` compara por **segmentos**, no `str.startswith`: `"org:4"` no es ancestro de
   `"org:42/..."` sólo porque el string es prefijo.
4. `PredicateRegistry`: un predicado no registrado es indeterminado, nunca un error; uno que
   lanza también.
5. `enforce_condition_limits` rechaza un árbol demasiado grande o demasiado profundo.
6. Ida y vuelta por JSON (`dump_condition`/`parse_condition`) preserva el árbol — es lo que
   hace posible guardar una condición en una columna `JSON` y compartir los mismos vectores
   con el cliente TypeScript (Fase F6).
7. Vectores dorados (`tests/fixtures/authz_vectors.json`): el mismo resultado que va a fijar
   el equivalente en vitest.
"""
from __future__ import annotations

import json
import typing as t
from pathlib import Path

import pytest
from pydantic import ValidationError

from hexcore.darwin.plugins.drbac.conditions import (
    And,
    Const,
    Contains,
    ConditionLimits,
    ConditionTooComplexError,
    Eq,
    EvaluationContext,
    Gt,
    Gte,
    In,
    InvalidVarPathError,
    Lt,
    Lte,
    MISSING,
    Ne,
    Not,
    Or,
    Predicate,
    StartsWith,
    TimeBetween,
    Var,
    WithinScope,
    compile_condition,
    condition_depth,
    count_nodes,
    dump_condition,
    enforce_condition_limits,
    evaluate_condition,
    parse_condition,
)
from hexcore.darwin.plugins.drbac.predicates import PredicateRegistry

FIXTURES = Path(__file__).parent / "fixtures"


def _contexto(datos: t.Mapping[str, t.Any]) -> EvaluationContext:
    return EvaluationContext(
        subject=datos.get("subject", {}),
        resource=datos.get("resource", {}),
        env=datos.get("env", {}),
    )


# ── `Var`: el allowlist de namespaces ─────────────────────────────────────────
@pytest.mark.parametrize(
    "path",
    ["subject.id", "resource.owner_id", "env.now", "resource.metadata.tier"],
)
def test_var_acepta_los_namespaces_permitidos(path: str) -> None:
    assert Var(path).path == path


@pytest.mark.parametrize("path", ["actor.id", "context.subject.id", "subject", "SUBJECT.id"])
def test_var_rechaza_cualquier_otro_namespace(path: str) -> None:
    with pytest.raises(InvalidVarPathError):
        Var(path)


def test_var_rechaza_path_vacio() -> None:
    # `min_length=1` de pydantic gana antes que el validador del allowlist: sigue siendo un
    # rechazo, sólo que con el tipo de excepción de pydantic en vez del propio del módulo.
    with pytest.raises(ValidationError):
        Var("")


def test_var_rechaza_namespace_sin_clave() -> None:
    with pytest.raises(InvalidVarPathError):
        Var("resource.")


# ── `Const`: coerción de literales ────────────────────────────────────────────
def test_eq_envuelve_un_literal_crudo_en_const() -> None:
    nodo = Eq(Var("resource.status"), "draft")
    assert nodo.right == Const("draft")


def test_const_con_una_lista_se_guarda_como_tupla() -> None:
    nodo = In(Var("resource.status"), ["draft", "pending"])
    assert nodo.right == Const(("draft", "pending"))


def test_and_no_envuelve_un_dict_ya_serializado_como_const() -> None:
    """Si `_as_value` envolviera un dict con forma de `Var` en un `Const`, la ida y vuelta por
    JSON rompería: un `{"type": "var", ...}` deserializado tiene que seguir siendo un `Var`."""
    reparsed = Eq.model_validate({
        "type": "eq",
        "left": {"type": "var", "path": "resource.id"},
        "right": {"type": "const", "value": "x"},
    })
    assert isinstance(reparsed.left, Var)
    assert reparsed.left.path == "resource.id"


# ── Semántica de cada nodo ─────────────────────────────────────────────────────
def test_eq_y_ne() -> None:
    ctx = _contexto({"resource": {"status": "draft"}})
    assert evaluate_condition(Eq(Var("resource.status"), "draft"), ctx) is True
    assert evaluate_condition(Ne(Var("resource.status"), "draft"), ctx) is False


@pytest.mark.parametrize(
    "clase,izquierda,derecha,esperado",
    [
        (Gt, 10, 5, True),
        (Gt, 5, 10, False),
        (Gte, 5, 5, True),
        (Lt, 5, 10, True),
        (Lte, 5, 5, True),
    ],
)
def test_comparaciones_de_orden(
    clase: type, izquierda: int, derecha: int, esperado: bool
) -> None:
    nodo = clase(Const(izquierda), Const(derecha))
    assert evaluate_condition(nodo, _contexto({})) is esperado


def test_comparacion_de_orden_con_tipos_incomparables_es_indeterminada() -> None:
    nodo = Gt(Const("no-es-numero"), Const(5))
    assert evaluate_condition(nodo, _contexto({})) is None


def test_in_y_contains_son_espejo() -> None:
    ctx = _contexto({"resource": {"status": "draft", "tags": ["urgent", "billing"]}})
    assert evaluate_condition(In(Var("resource.status"), ["draft", "pending"]), ctx) is True
    assert evaluate_condition(Contains(Var("resource.tags"), "urgent"), ctx) is True
    assert evaluate_condition(Contains(Var("resource.tags"), "vip"), ctx) is False


def test_in_con_contenedor_no_iterable_es_indeterminado() -> None:
    nodo = In(Const("x"), Const(5))
    assert evaluate_condition(nodo, _contexto({})) is None


def test_starts_with() -> None:
    ctx = _contexto({"resource": {"scope_path": "org:42/project:7"}})
    assert evaluate_condition(StartsWith(Var("resource.scope_path"), "org:42"), ctx) is True
    assert evaluate_condition(StartsWith(Var("resource.scope_path"), "org:9"), ctx) is False


def test_starts_with_con_tipos_no_string_es_indeterminado() -> None:
    assert evaluate_condition(StartsWith(Const(5), Const("x")), _contexto({})) is None


# ── `WithinScope`: por segmentos, no por string crudo ─────────────────────────
@pytest.mark.parametrize(
    "path,ancestro,esperado",
    [
        ("org:42/project:7", "org:42", True),
        ("org:42", "org:42", True),
        ("org:42/project:7", "", True),
        ("org:42/project:7", "org:4", False),  # el caso de la tabla de riesgos del plan
        ("org:42/project:8", "org:42/project:7", False),
        ("org:9", "org:42", False),
    ],
)
def test_within_scope_compara_por_segmentos(
    path: str, ancestro: str, esperado: bool
) -> None:
    nodo = WithinScope(Const(path), Const(ancestro))
    assert evaluate_condition(nodo, _contexto({})) is esperado


# ── `TimeBetween` ──────────────────────────────────────────────────────────────
def test_time_between_dentro_y_fuera_de_la_ventana() -> None:
    ventana = TimeBetween(
        Var("env.now"),
        Const("2026-01-01T09:00:00+00:00"),
        Const("2026-01-01T18:00:00+00:00"),
    )
    dentro = _contexto({"env": {"now": "2026-01-01T12:00:00+00:00"}})
    fuera = _contexto({"env": {"now": "2026-01-01T20:00:00+00:00"}})
    assert evaluate_condition(ventana, dentro) is True
    assert evaluate_condition(ventana, fuera) is False


def test_time_between_con_fecha_invalida_es_indeterminado() -> None:
    ventana = TimeBetween(Var("env.now"), Const("no-es-fecha"), Const("2026-01-01T18:00:00+00:00"))
    ctx = _contexto({"env": {"now": "2026-01-01T12:00:00+00:00"}})
    assert evaluate_condition(ventana, ctx) is None


# ── Variables faltantes: indeterminado, nunca una excepción ───────────────────
def test_var_faltante_es_indeterminado_no_una_excepcion() -> None:
    nodo = Eq(Var("resource.no_existe"), "draft")
    assert evaluate_condition(nodo, _contexto({"resource": {"status": "draft"}})) is None


def test_var_con_nivel_intermedio_que_no_es_mapping_es_indeterminado() -> None:
    nodo = Eq(Var("resource.status.anidado"), "x")
    assert evaluate_condition(nodo, _contexto({"resource": {"status": "draft"}})) is None


# ── Lógica de Kleene en `And`/`Or`/`Not` ──────────────────────────────────────
def test_and_false_domina_sobre_indeterminado() -> None:
    nodo = And(Eq(Const(1), Const(2)), Eq(Var("resource.no_existe"), Const(1)))
    assert evaluate_condition(nodo, _contexto({})) is False


def test_and_indeterminado_sin_ningun_false_es_indeterminado() -> None:
    nodo = And(Eq(Const(1), Const(1)), Eq(Var("resource.no_existe"), Const(1)))
    assert evaluate_condition(nodo, _contexto({})) is None


def test_or_true_domina_sobre_indeterminado() -> None:
    nodo = Or(Eq(Const(1), Const(1)), Eq(Var("resource.no_existe"), Const(1)))
    assert evaluate_condition(nodo, _contexto({})) is True


def test_or_indeterminado_sin_ningun_true_es_indeterminado() -> None:
    nodo = Or(Eq(Const(1), Const(2)), Eq(Var("resource.no_existe"), Const(1)))
    assert evaluate_condition(nodo, _contexto({})) is None


def test_not_invierte_pero_deja_el_indeterminado_intacto() -> None:
    assert evaluate_condition(Not(Eq(Const(1), Const(1))), _contexto({})) is False
    assert evaluate_condition(Not(Eq(Var("resource.no_existe"), Const(1))), _contexto({})) is None


# ── `Predicate` ────────────────────────────────────────────────────────────────
def test_predicate_no_registrado_es_indeterminado() -> None:
    registro = PredicateRegistry()
    assert evaluate_condition(Predicate("no_existe"), _contexto({}), predicates=registro) is None


def test_predicate_registrado_recibe_los_argumentos_ya_resueltos() -> None:
    registro = PredicateRegistry()
    llamadas: list[tuple[t.Any, ...]] = []

    @registro.predicate("min_amount")
    def _min_amount(context: EvaluationContext, umbral: float) -> bool:
        llamadas.append((context, umbral))
        return context.resource.get("amount", 0) >= umbral

    ctx = _contexto({"resource": {"amount": 1500}})
    nodo = Predicate("min_amount", Const(1000))
    assert evaluate_condition(nodo, ctx, predicates=registro) is True
    assert llamadas == [(ctx, 1000)]


def test_predicate_con_argumento_faltante_es_indeterminado_sin_llamar_la_funcion() -> None:
    registro = PredicateRegistry()
    llamado = False

    @registro.predicate("cualquiera")
    def _fn(context: EvaluationContext, valor: t.Any) -> bool:
        nonlocal llamado
        llamado = True
        return True

    nodo = Predicate("cualquiera", Var("resource.no_existe"))
    assert evaluate_condition(nodo, _contexto({}), predicates=registro) is None
    assert llamado is False


def test_predicate_que_lanza_es_indeterminado_no_una_excepcion() -> None:
    registro = PredicateRegistry()

    @registro.predicate("explota")
    def _explota(context: EvaluationContext) -> bool:
        raise RuntimeError("una API externa caída, por ejemplo")

    assert evaluate_condition(Predicate("explota"), _contexto({}), predicates=registro) is None


def test_registro_de_predicado_duplicado_lanza() -> None:
    registro = PredicateRegistry()
    registro.predicate("x")(lambda context: True)
    with pytest.raises(ValueError, match="x"):
        registro.predicate("x")(lambda context: False)


def test_unregister_no_lanza_si_no_estaba() -> None:
    registro = PredicateRegistry()
    registro.unregister("no-estaba")  # no debería lanzar


# ── Límites de tamaño ──────────────────────────────────────────────────────────
def test_enforce_condition_limits_acepta_un_arbol_chico() -> None:
    nodo = And(Eq(Const(1), Const(1)), Eq(Const(2), Const(2)))
    enforce_condition_limits(nodo, ConditionLimits(max_nodes=10, max_depth=5))  # no debería lanzar


def test_enforce_condition_limits_rechaza_demasiados_nodos() -> None:
    nodo = And(*[Eq(Const(1), Const(1)) for _ in range(20)])
    with pytest.raises(ConditionTooComplexError):
        enforce_condition_limits(nodo, ConditionLimits(max_nodes=10, max_depth=100))


def test_enforce_condition_limits_rechaza_demasiada_profundidad() -> None:
    nodo: t.Any = Eq(Const(1), Const(1))
    for _ in range(20):
        nodo = Not(nodo)
    with pytest.raises(ConditionTooComplexError):
        enforce_condition_limits(nodo, ConditionLimits(max_nodes=1000, max_depth=5))


def test_compile_condition_aplica_los_limites_por_default() -> None:
    nodo = And(*[Eq(Const(1), Const(1)) for _ in range(300)])
    with pytest.raises(ConditionTooComplexError):
        compile_condition(nodo)


def test_count_nodes_y_condition_depth() -> None:
    hoja = Eq(Const(1), Const(1))
    assert count_nodes(hoja) == 1
    assert condition_depth(hoja) == 1

    arbol = And(hoja, Not(hoja))
    assert count_nodes(arbol) == 4  # And + Eq + Not + el Eq de adentro del Not
    assert condition_depth(arbol) == 3  # And -> Not -> Eq


# ── Ida y vuelta por JSON ──────────────────────────────────────────────────────
def test_dump_y_parse_condition_son_inversos() -> None:
    original = Or(
        And(
            Eq(Var("resource.owner_id"), Var("subject.id")),
            In(Var("resource.status"), ["draft"]),
        ),
        Predicate("business_hours"),
    )
    volcado = dump_condition(original)
    # Es JSON-safe de verdad: round-trip por `json.dumps`/`json.loads`, no sólo un dict Python.
    reparsed = parse_condition(json.loads(json.dumps(volcado)))
    assert reparsed == original


def test_construir_desde_json_crudo_valida_el_allowlist_de_var() -> None:
    with pytest.raises((InvalidVarPathError, ValidationError)):
        parse_condition({
            "type": "eq",
            "left": {"type": "var", "path": "algo.prohibido"},
            "right": {"type": "const", "value": 1},
        })


# ── Vectores dorados, compartidos con el cliente TypeScript (Fase F6) ─────────
def _cargar_vectores() -> list[dict[str, t.Any]]:
    contenido = json.loads((FIXTURES / "authz_vectors.json").read_text(encoding="utf-8"))
    return contenido["vectors"]


@pytest.mark.parametrize("vector", _cargar_vectores(), ids=lambda v: v["name"])
def test_vectores_dorados(vector: dict[str, t.Any]) -> None:
    condicion = parse_condition(vector["condition"])
    contexto = _contexto(vector["context"])
    assert evaluate_condition(condicion, contexto) == vector["expected"]


def test_hay_al_menos_un_vector_por_cada_tipo_de_nodo() -> None:
    """Que nadie borre un vector y se quede sin cobertura de ese nodo sin darse cuenta."""
    tipos_esperados = {
        "eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "starts_with",
        "within_scope", "time_between", "and", "or", "not",
    }

    def _tipos_en(nodo: t.Any, acumulado: set[str]) -> None:
        if isinstance(nodo, dict):
            tipo = nodo.get("type")
            if tipo:
                acumulado.add(tipo)
            for valor in nodo.values():
                _tipos_en(valor, acumulado)
        elif isinstance(nodo, list):
            for item in nodo:
                _tipos_en(item, acumulado)

    vistos: set[str] = set()
    for vector in _cargar_vectores():
        _tipos_en(vector["condition"], vistos)

    faltantes = tipos_esperados - vistos
    assert not faltantes, f"faltan vectores para: {sorted(faltantes)}"


def test_missing_sentinel_no_es_none() -> None:
    """`MISSING` tiene que ser distinguible de un `Var` que resolvió a `None` de verdad."""
    ctx = _contexto({"resource": {"campo_nulo": None}})
    # `campo_nulo` está presente y vale None: NO es indeterminado, `Eq` contra `Const(None)` da True.
    assert evaluate_condition(Eq(Var("resource.campo_nulo"), Const(None)), ctx) is True
    assert MISSING is not None
