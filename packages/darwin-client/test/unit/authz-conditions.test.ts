import { describe, expect, it } from "vitest";
// Comparte el mismo fixture que consume `test_darwin_drbac_conditions.py` en el paquete
// hermano `packages/hexcore` — el DoD de la Fase F6 es que las dos runtimes coincidan.
import vectoresCrudos from "../../../hexcore/tests/fixtures/authz_vectors.json";
import {
  type Condition,
  compileCondition,
  type EvaluationContext,
  evaluateCondition,
} from "../../src/authz/conditions";

function ctx(datos: Partial<EvaluationContext>): EvaluationContext {
  return { subject: {}, resource: {}, env: {}, ...datos };
}

// ── Cada nodo, por su cuenta ───────────────────────────────────────────────────
describe("evaluateCondition — cada nodo", () => {
  it("eq/ne", () => {
    const contexto = ctx({ resource: { status: "draft" } });
    expect(
      evaluateCondition(
        {
          type: "eq",
          left: { type: "var", path: "resource.status" },
          right: { type: "const", value: "draft" },
        },
        contexto,
      ),
    ).toBe(true);
    expect(
      evaluateCondition(
        {
          type: "ne",
          left: { type: "var", path: "resource.status" },
          right: { type: "const", value: "draft" },
        },
        contexto,
      ),
    ).toBe(false);
  });

  it.each([
    ["gt", 10, 5, true],
    ["gt", 5, 10, false],
    ["gt", 5, 5, false],
    ["gte", 5, 5, true],
    ["lt", 5, 10, true],
    ["lte", 5, 5, true],
  ] as const)("%s(%d, %d) -> %s", (tipo, izquierda, derecha, esperado) => {
    const nodo = {
      type: tipo,
      left: { type: "const", value: izquierda },
      right: { type: "const", value: derecha },
    } as Condition;
    expect(evaluateCondition(nodo, ctx({}))).toBe(esperado);
  });

  it("una comparación de orden con tipos incomparables es indeterminada", () => {
    const nodo: Condition = {
      type: "gt",
      left: { type: "const", value: "no-es-numero" },
      right: { type: "const", value: 5 },
    };
    expect(evaluateCondition(nodo, ctx({}))).toBeNull();
  });

  it("in/contains son espejo", () => {
    const contexto = ctx({ resource: { status: "draft", tags: ["urgent", "billing"] } });
    expect(
      evaluateCondition(
        {
          type: "in",
          left: { type: "var", path: "resource.status" },
          right: { type: "const", value: ["draft", "pending"] },
        },
        contexto,
      ),
    ).toBe(true);
    expect(
      evaluateCondition(
        {
          type: "contains",
          left: { type: "var", path: "resource.tags" },
          right: { type: "const", value: "urgent" },
        },
        contexto,
      ),
    ).toBe(true);
    expect(
      evaluateCondition(
        {
          type: "contains",
          left: { type: "var", path: "resource.tags" },
          right: { type: "const", value: "vip" },
        },
        contexto,
      ),
    ).toBe(false);
  });

  it("starts_with", () => {
    const contexto = ctx({ resource: { scopePath: "org:42/project:7" } });
    expect(
      evaluateCondition(
        {
          type: "starts_with",
          left: { type: "var", path: "resource.scopePath" },
          right: { type: "const", value: "org:42" },
        },
        contexto,
      ),
    ).toBe(true);
  });

  it.each([
    ["org:42/project:7", "org:42", true],
    ["org:42", "org:42", true],
    ["org:42/project:7", "", true],
    // El caso de la tabla de riesgos del plan: "org:4" no es ancestro de "org:42/..." aunque
    // el string sea prefijo — la comparación es por segmentos.
    ["org:42/project:7", "org:4", false],
    ["org:42/project:8", "org:42/project:7", false],
  ])("within_scope(%s, %s) -> %s", (path, ancestro, esperado) => {
    const nodo: Condition = {
      type: "within_scope",
      left: { type: "const", value: path },
      right: { type: "const", value: ancestro },
    };
    expect(evaluateCondition(nodo, ctx({}))).toBe(esperado);
  });

  it("time_between dentro y fuera de la ventana", () => {
    const ventana: Condition = {
      type: "time_between",
      value: { type: "var", path: "env.now" },
      start: { type: "const", value: "2026-01-01T09:00:00+00:00" },
      end: { type: "const", value: "2026-01-01T18:00:00+00:00" },
    };
    expect(
      evaluateCondition(ventana, ctx({ env: { now: "2026-01-01T12:00:00+00:00" } })),
    ).toBe(true);
    expect(
      evaluateCondition(ventana, ctx({ env: { now: "2026-01-01T20:00:00+00:00" } })),
    ).toBe(false);
  });

  it("una variable faltante es indeterminada, no una excepción", () => {
    const nodo: Condition = {
      type: "eq",
      left: { type: "var", path: "resource.no_existe" },
      right: { type: "const", value: "draft" },
    };
    expect(evaluateCondition(nodo, ctx({ resource: { status: "draft" } }))).toBeNull();
  });

  it("un nivel intermedio que no es un objeto es indeterminado", () => {
    const nodo: Condition = {
      type: "eq",
      left: { type: "var", path: "resource.status.anidado" },
      right: { type: "const", value: "x" },
    };
    expect(evaluateCondition(nodo, ctx({ resource: { status: "draft" } }))).toBeNull();
  });

  it("un Predicate siempre es indeterminado — nunca llega desde el servidor, pero no rompe", () => {
    const nodo: Condition = { type: "predicate", name: "business_hours", args: [] };
    expect(evaluateCondition(nodo, ctx({}))).toBeNull();
  });
});

// ── Lógica de Kleene ────────────────────────────────────────────────────────────
describe("evaluateCondition — and/or/not (Kleene)", () => {
  const cierto: Condition = {
    type: "eq",
    left: { type: "const", value: 1 },
    right: { type: "const", value: 1 },
  };
  const falso: Condition = {
    type: "eq",
    left: { type: "const", value: 1 },
    right: { type: "const", value: 2 },
  };
  const indeterminado: Condition = {
    type: "eq",
    left: { type: "var", path: "resource.no_existe" },
    right: { type: "const", value: 1 },
  };

  it("and: false domina sobre indeterminado", () => {
    expect(evaluateCondition({ type: "and", items: [falso, indeterminado] }, ctx({}))).toBe(
      false,
    );
  });

  it("and: sin ningún false, indeterminado si hay alguno", () => {
    expect(
      evaluateCondition({ type: "and", items: [cierto, indeterminado] }, ctx({})),
    ).toBeNull();
  });

  it("and: todos true es true", () => {
    expect(evaluateCondition({ type: "and", items: [cierto, cierto] }, ctx({}))).toBe(true);
  });

  it("or: true domina sobre indeterminado", () => {
    expect(evaluateCondition({ type: "or", items: [cierto, indeterminado] }, ctx({}))).toBe(
      true,
    );
  });

  it("or: sin ningún true, indeterminado si hay alguno", () => {
    expect(
      evaluateCondition({ type: "or", items: [falso, indeterminado] }, ctx({})),
    ).toBeNull();
  });

  it("not invierte, pero deja el indeterminado intacto", () => {
    expect(evaluateCondition({ type: "not", item: cierto }, ctx({}))).toBe(false);
    expect(evaluateCondition({ type: "not", item: indeterminado }, ctx({}))).toBeNull();
  });
});

describe("compileCondition", () => {
  it("compila una vez y se puede reevaluar contra distintos contextos", () => {
    const evaluar = compileCondition({
      type: "eq",
      left: { type: "var", path: "resource.owner_id" },
      right: { type: "var", path: "subject.id" },
    });
    expect(evaluar(ctx({ subject: { id: "u1" }, resource: { owner_id: "u1" } }))).toBe(
      true,
    );
    expect(evaluar(ctx({ subject: { id: "u1" }, resource: { owner_id: "u2" } }))).toBe(
      false,
    );
  });
});

// ── Vectores dorados, compartidos con pytest ──────────────────────────────────
// Mismo archivo que `packages/hexcore/tests/test_darwin_drbac_conditions.py` consume: el DoD
// de la Fase F6 es que las dos runtimes den el mismo resultado sobre los mismos vectores.
interface VectorDorado {
  name: string;
  condition: Condition;
  context: {
    subject?: Record<string, unknown>;
    resource?: Record<string, unknown>;
    env?: Record<string, unknown>;
  };
  expected: boolean | null;
}

const vectors = (vectoresCrudos as { vectors: VectorDorado[] }).vectors;

describe("vectores dorados (packages/hexcore/tests/fixtures/authz_vectors.json)", () => {
  it("el fixture tiene al menos un vector por cada tipo de nodo", () => {
    const tiposEsperados = new Set([
      "eq",
      "ne",
      "gt",
      "gte",
      "lt",
      "lte",
      "in",
      "contains",
      "starts_with",
      "within_scope",
      "time_between",
      "and",
      "or",
      "not",
    ]);
    const vistos = new Set<string>();
    const recorrer = (valor: unknown): void => {
      if (Array.isArray(valor)) {
        for (const item of valor) recorrer(item);
        return;
      }
      if (valor !== null && typeof valor === "object") {
        const registro = valor as Record<string, unknown>;
        if (typeof registro.type === "string") vistos.add(registro.type);
        for (const clave of Object.keys(registro)) recorrer(registro[clave]);
      }
    };
    for (const vector of vectors) recorrer(vector.condition);

    const faltantes = [...tiposEsperados].filter((t) => !vistos.has(t));
    expect(faltantes).toEqual([]);
  });

  it.each(vectors.map((v) => [v.name, v] as const))("%s", (_nombre, vector) => {
    const contexto = ctx(vector.context);
    expect(evaluateCondition(vector.condition, contexto)).toBe(vector.expected);
  });
});
