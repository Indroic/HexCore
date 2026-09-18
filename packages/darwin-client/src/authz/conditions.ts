/**
 * `evaluateCondition()`: paridad con el AST de `hexcore/darwin/plugins/drbac/conditions.py`
 * (Fase F4 del plan rbac/drbac). Los mismos vectores dorados
 * (`packages/hexcore/tests/fixtures/authz_vectors.json`) corren en pytest y en este módulo —
 * ver `test/unit/authz-conditions.test.ts`.
 *
 * **Este módulo nunca construye una condición, sólo la evalúa.** El AST siempre llega desde el
 * servidor —`GET /auth/drbac/me/snapshot`, Fase F5— ya validado allá (el allowlist de `Var`,
 * los límites de tamaño): acá no hace falta re-validar la forma, sólo evaluarla de manera
 * optimista para la interfaz. La autoridad sigue siendo `POST /auth/drbac/check` contra el
 * servidor.
 *
 * **Nunca hay `Predicate` en lo que llega acá.** El servidor fuerza `client_evaluable = false`
 * en cualquier regla cuya condición use un `Predicate` (ver `DrbacService._preparar_reglas`
 * del lado Python) — un predicado sólo se puede evaluar corriendo el mismo código Python, y
 * `/me/snapshot` nunca manda una regla que este módulo no pueda resolver por su cuenta. El tipo
 * sigue estando declarado acá por fidelidad estructural con el AST compartido, pero
 * `compileCondition` lo trata como indeterminado si alguna vez aparece.
 *
 * **Semántica tri-estado (lógica de Kleene, igual que del lado Python).** `true`/`false`
 * deciden; `null` es "indeterminate" — una variable que no resuelve, un predicado, tipos no
 * comparables. `And`/`Or`/`Not` propagan ese tercer estado con las mismas reglas que el `NULL`
 * de SQL: un resultado que ya domina (`false` en `And`, `true` en `Or`) gana sobre cualquier
 * indeterminado de sus hermanos.
 */

const NAMESPACES = ["subject", "resource", "env"] as const;

export type ConstScalar = boolean | number | string | null;

export interface Var {
  type: "var";
  path: string;
}

export interface Const {
  type: "const";
  value: ConstScalar | ConstScalar[];
}

export type Value = Var | Const;

interface BinaryValueCondition {
  left: Value;
  right: Value;
}

export interface Eq extends BinaryValueCondition {
  type: "eq";
}
export interface Ne extends BinaryValueCondition {
  type: "ne";
}
export interface Gt extends BinaryValueCondition {
  type: "gt";
}
export interface Gte extends BinaryValueCondition {
  type: "gte";
}
export interface Lt extends BinaryValueCondition {
  type: "lt";
}
export interface Lte extends BinaryValueCondition {
  type: "lte";
}
/** `left` está entre los valores de `right` (que suele resolver a una lista). */
export interface In extends BinaryValueCondition {
  type: "in";
}
/** `right` está adentro de `left` (una lista o un string) — el espejo de `In`. */
export interface Contains extends BinaryValueCondition {
  type: "contains";
}
export interface StartsWith extends BinaryValueCondition {
  type: "starts_with";
}
/** `left` (un `scope_path`) está en o debajo de `right` (el ancestro) — por segmentos. */
export interface WithinScope extends BinaryValueCondition {
  type: "within_scope";
}

export interface TimeBetween {
  type: "time_between";
  value: Value;
  start: Value;
  end: Value;
}

export interface And {
  type: "and";
  items: Condition[];
}

export interface Or {
  type: "or";
  items: Condition[];
}

export interface Not {
  type: "not";
  item: Condition;
}

/** Nunca llega poblado desde `/me/snapshot` — ver el docstring del módulo. */
export interface Predicate {
  type: "predicate";
  name: string;
  args: Value[];
}

export type Condition =
  | Eq
  | Ne
  | Gt
  | Gte
  | Lt
  | Lte
  | In
  | Contains
  | StartsWith
  | WithinScope
  | TimeBetween
  | And
  | Or
  | Not
  | Predicate;

export interface EvaluationContext {
  subject: Record<string, unknown>;
  resource: Record<string, unknown>;
  env: Record<string, unknown>;
}

/**
 * Sentinela: un `Var` no se pudo resolver. Deliberadamente no es `null` ni `undefined`: un
 * atributo puede legítimamente valer `null`, y confundir "no está" con "vale null" convertiría
 * un dato ausente en un `Eq(Var(...), Const(null))` que da `true` por accidente.
 */
const MISSING: unique symbol = Symbol("darwin.drbac.missing");
type Missing = typeof MISSING;

/** `true`/`false` deciden; `null` es indeterminado — ver el docstring del módulo. */
export type ConditionResult = boolean | null;

export type ConditionEvaluator = (context: EvaluationContext) => ConditionResult;
type ValueEvaluator = (context: EvaluationContext) => unknown | Missing;

function namespaceOf(
  context: EvaluationContext,
  namespace: string,
): Record<string, unknown> {
  // `if`/`else if` explícito y no `context[namespace]`: mismo criterio que el lado Python
  // (`_namespace_de`) — sin lectura dirigida por un string que no sea un literal en el propio
  // código, aunque acá `namespace` ya viene de un AST que el servidor validó.
  if (namespace === "subject") return context.subject;
  if (namespace === "resource") return context.resource;
  if (namespace === "env") return context.env;
  return {};
}

function compileValue(node: Value): ValueEvaluator {
  if (node.type === "const") {
    const { value } = node;
    return () => value;
  }

  const [namespace = "", ...resto] = node.path.split(".");
  return (context: EvaluationContext): unknown | Missing => {
    if (!NAMESPACES.includes(namespace as (typeof NAMESPACES)[number])) return MISSING;
    let actual: unknown = namespaceOf(context, namespace);
    for (const clave of resto) {
      if (typeof actual !== "object" || actual === null || Array.isArray(actual))
        return MISSING;
      const mapa = actual as Record<string, unknown>;
      if (!(clave in mapa)) return MISSING;
      actual = mapa[clave];
    }
    return actual;
  };
}

function asDate(valor: unknown): Date | null {
  if (valor instanceof Date) return Number.isNaN(valor.getTime()) ? null : valor;
  if (typeof valor === "string") {
    const fecha = new Date(valor);
    return Number.isNaN(fecha.getTime()) ? null : fecha;
  }
  return null;
}

function ordered(
  a: unknown,
  b: unknown,
): readonly [number, number] | readonly [string, string] | null {
  if (typeof a === "number" && typeof b === "number") return [a, b];
  if (typeof a === "string" && typeof b === "string") return [a, b];
  return null;
}

function compileComparison(
  node: BinaryValueCondition,
  operador: (a: number | string, b: number | string) => boolean,
): ConditionEvaluator {
  const izquierda = compileValue(node.left);
  const derecha = compileValue(node.right);
  return (context) => {
    const valorIzq = izquierda(context);
    const valorDer = derecha(context);
    if (valorIzq === MISSING || valorDer === MISSING) return null;
    const par = ordered(valorIzq, valorDer);
    if (par === null) return null;
    return operador(par[0], par[1]);
  };
}

function compileEquality(node: BinaryValueCondition, negar: boolean): ConditionEvaluator {
  const izquierda = compileValue(node.left);
  const derecha = compileValue(node.right);
  return (context) => {
    const valorIzq = izquierda(context);
    const valorDer = derecha(context);
    if (valorIzq === MISSING || valorDer === MISSING) return null;
    const iguales = valorIzq === valorDer;
    return negar ? !iguales : iguales;
  };
}

function compileMembership(
  node: BinaryValueCondition,
  invertido: boolean,
): ConditionEvaluator {
  const izquierda = compileValue(node.left);
  const derecha = compileValue(node.right);
  return (context) => {
    const valorIzq = izquierda(context);
    const valorDer = derecha(context);
    if (valorIzq === MISSING || valorDer === MISSING) return null;
    const [contenedor, elemento] = invertido ? [valorIzq, valorDer] : [valorDer, valorIzq];
    if (typeof contenedor === "string") {
      return typeof elemento === "string" ? contenedor.includes(elemento) : null;
    }
    if (Array.isArray(contenedor)) {
      return contenedor.some((v) => v === elemento);
    }
    return null;
  };
}

function compileStartsWith(node: StartsWith): ConditionEvaluator {
  const izquierda = compileValue(node.left);
  const derecha = compileValue(node.right);
  return (context) => {
    const valor = izquierda(context);
    const prefijo = derecha(context);
    if (valor === MISSING || prefijo === MISSING) return null;
    if (typeof valor !== "string" || typeof prefijo !== "string") return null;
    return valor.startsWith(prefijo);
  };
}

/** Por segmentos, no `String.prototype.startsWith` crudo — mismo criterio que el lado Python. */
function dentroDeScope(path: string, ancestro: string): boolean {
  if (ancestro === "") return true;
  const segmentosPath = path ? path.split("/") : [];
  const segmentosAncestro = ancestro ? ancestro.split("/") : [];
  if (segmentosAncestro.length > segmentosPath.length) return false;
  return segmentosAncestro.every((segmento, i) => segmento === segmentosPath[i]);
}

function compileWithinScope(node: WithinScope): ConditionEvaluator {
  const izquierda = compileValue(node.left);
  const derecha = compileValue(node.right);
  return (context) => {
    const path = izquierda(context);
    const ancestro = derecha(context);
    if (path === MISSING || ancestro === MISSING) return null;
    if (typeof path !== "string" || typeof ancestro !== "string") return null;
    return dentroDeScope(path, ancestro);
  };
}

function compileTimeBetween(node: TimeBetween): ConditionEvaluator {
  const valorEv = compileValue(node.value);
  const inicioEv = compileValue(node.start);
  const finEv = compileValue(node.end);
  return (context) => {
    const crudoValor = valorEv(context);
    const crudoInicio = inicioEv(context);
    const crudoFin = finEv(context);
    if (crudoValor === MISSING || crudoInicio === MISSING || crudoFin === MISSING)
      return null;
    const valor = asDate(crudoValor);
    const inicio = asDate(crudoInicio);
    const fin = asDate(crudoFin);
    if (valor === null || inicio === null || fin === null) return null;
    return inicio.getTime() <= valor.getTime() && valor.getTime() <= fin.getTime();
  };
}

function and(compilados: ConditionEvaluator[]): ConditionEvaluator {
  return (context) => {
    let vioIndeterminado = false;
    for (const hijo of compilados) {
      const resultado = hijo(context);
      if (resultado === false) return false; // domina: no hace falta seguir evaluando.
      if (resultado === null) vioIndeterminado = true;
    }
    return vioIndeterminado ? null : true;
  };
}

function or(compilados: ConditionEvaluator[]): ConditionEvaluator {
  return (context) => {
    let vioIndeterminado = false;
    for (const hijo of compilados) {
      const resultado = hijo(context);
      if (resultado === true) return true; // domina.
      if (resultado === null) vioIndeterminado = true;
    }
    return vioIndeterminado ? null : false;
  };
}

function not(compilado: ConditionEvaluator): ConditionEvaluator {
  return (context) => {
    const resultado = compilado(context);
    return resultado === null ? null : !resultado;
  };
}

/** Siempre indeterminado: ver el docstring del módulo sobre por qué nunca hace falta un registro. */
function compilePredicate(): ConditionEvaluator {
  return () => null;
}

/**
 * Compila `node` a una función pura `EvaluationContext -> true|false|null`.
 *
 * Uso::
 *
 *     const evaluar = compileCondition(regla.condition);
 *     evaluar({ subject: { id: "u1" }, resource: { ownerId: "u1" }, env: {} }); // true
 */
export function compileCondition(node: Condition): ConditionEvaluator {
  switch (node.type) {
    case "eq":
      return compileEquality(node, false);
    case "ne":
      return compileEquality(node, true);
    case "gt":
      return compileComparison(node, (a, b) => a > b);
    case "gte":
      return compileComparison(node, (a, b) => a >= b);
    case "lt":
      return compileComparison(node, (a, b) => a < b);
    case "lte":
      return compileComparison(node, (a, b) => a <= b);
    case "in":
      return compileMembership(node, false);
    case "contains":
      return compileMembership(node, true);
    case "starts_with":
      return compileStartsWith(node);
    case "within_scope":
      return compileWithinScope(node);
    case "time_between":
      return compileTimeBetween(node);
    case "and":
      return and(node.items.map(compileCondition));
    case "or":
      return or(node.items.map(compileCondition));
    case "not":
      return not(compileCondition(node.item));
    case "predicate":
      return compilePredicate();
  }
}

/** Compila y evalúa en un solo paso. Para el camino frío o un chequeo de una sola vez. */
export function evaluateCondition(
  node: Condition,
  context: EvaluationContext,
): ConditionResult {
  return compileCondition(node)(context);
}
