import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineAccessControl } from "../../src/authz/schema";
import { createDarwinClient } from "../../src/core/client";
import { drbac } from "../../src/plugins/drbac";
import { rbac } from "../../src/plugins/rbac";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { createFakeFetch, jsonResponse } from "./helpers/fake-fetch";

const ac = defineAccessControl({
  resources: { invoice: ["read", "approve"], user: ["delete"] },
});

interface SnapshotFixture {
  version: number;
  rules: Array<{
    effect: "allow" | "deny";
    actions: string[];
    resource_type: string;
    condition: unknown;
  }>;
}

function makeFakeServer(snapshots: Record<string, SnapshotFixture>) {
  const checkCalls: Array<{ items: unknown[] }> = [];

  const { fetchImpl, calls } = createFakeFetch(async (url, init) => {
    const parsedUrl = new URL(url);
    if (parsedUrl.pathname.endsWith("/auth/drbac/me/snapshot")) {
      const scope = parsedUrl.searchParams.get("scope") ?? "";
      const fixture = snapshots[scope] ?? { version: 1, rules: [] };
      return jsonResponse({
        body: {
          scope,
          version: fixture.version,
          expires_at: "2026-01-01T00:10:00.000Z",
          rules: fixture.rules,
        },
      });
    }
    if (parsedUrl.pathname.endsWith("/auth/drbac/check")) {
      const body = JSON.parse(String(init?.body)) as {
        items: Array<{ action: string; resource_type: string; resource_id: string | null }>;
      };
      checkCalls.push(body);
      return jsonResponse({
        body: body.items.map((item) => ({
          action: item.action,
          resource_type: item.resource_type,
          resource_id: item.resource_id,
          allowed: item.action === "invoice.approve",
        })),
      });
    }
    if (parsedUrl.pathname.endsWith("/auth/rbac/me/permissions")) {
      return jsonResponse({
        body: {
          scope: "",
          roles: [],
          permissions: [],
          version: 1,
          expires_at: "2026-01-01T00:10:00.000Z",
        },
      });
    }
    throw new Error(`ruta no simulada: ${parsedUrl.pathname}`);
  });

  return { fetchImpl, calls, checkCalls };
}

function nuevoCliente(fetchImpl: typeof fetch) {
  return createDarwinClient({
    baseUrl: "https://api.test",
    transport: new BearerTransport({ storage: memoryStorage() }),
    fetch: fetchImpl,
    hydrateOnCreate: false,
    plugins: [rbac({ ac }), drbac({ ac })],
  });
}

describe("plugin drbac", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("evaluate() responde 'unknown' fail-closed antes de que el snapshot resuelva", () => {
    const { fetchImpl } = makeFakeServer({ "": { version: 1, rules: [] } });
    const client = nuevoCliente(fetchImpl);

    expect(client.drbac.evaluate("invoice", "approve")).toBe("unknown");
  });

  it("evaluate() refleja una regla deny cuya condición da true", async () => {
    const { fetchImpl } = makeFakeServer({
      "": {
        version: 1,
        rules: [
          {
            effect: "deny",
            actions: ["invoice.approve"],
            resource_type: "invoice",
            condition: {
              type: "eq",
              left: { type: "var", path: "resource.owner_id" },
              right: { type: "const", value: "u1" },
            },
          },
        ],
      },
    });
    const client = nuevoCliente(fetchImpl);
    await client.drbac.revalidate();

    expect(client.drbac.evaluate("invoice", "approve", { ownerId: "u1" })).toBe("deny");
    expect(client.drbac.evaluate("invoice", "approve", { ownerId: "u2" })).toBe("unknown");
  });

  it("evaluate() refleja una regla allow sin condición", async () => {
    const { fetchImpl } = makeFakeServer({
      "": {
        version: 1,
        rules: [
          {
            effect: "allow",
            actions: ["invoice.read"],
            resource_type: "invoice",
            condition: null,
          },
        ],
      },
    });
    const client = nuevoCliente(fetchImpl);
    await client.drbac.revalidate();

    expect(client.drbac.evaluate("invoice", "read")).toBe("allow");
  });

  it("evaluate() sobre un recurso o acción no relacionados es 'unknown'", async () => {
    const { fetchImpl } = makeFakeServer({
      "": {
        version: 1,
        rules: [
          {
            effect: "allow",
            actions: ["invoice.read"],
            resource_type: "invoice",
            condition: null,
          },
        ],
      },
    });
    const client = nuevoCliente(fetchImpl);
    await client.drbac.revalidate();

    expect(client.drbac.evaluate("user", "delete")).toBe("unknown");
  });

  it("evaluate() respeta el scope explícito", async () => {
    const { fetchImpl } = makeFakeServer({
      "": { version: 1, rules: [] },
      "org:1": {
        version: 1,
        rules: [
          {
            effect: "allow",
            actions: ["invoice.read"],
            resource_type: "invoice",
            condition: null,
          },
        ],
      },
    });
    const client = nuevoCliente(fetchImpl);
    await client.drbac.revalidate({ scope: "org:1" });

    expect(client.drbac.evaluate("invoice", "read")).toBe("unknown");
    expect(client.drbac.evaluate("invoice", "read", { scope: "org:1" })).toBe("allow");
  });

  it("check() es autoritativo contra el servidor", async () => {
    const { fetchImpl } = makeFakeServer({ "": { version: 1, rules: [] } });
    const client = nuevoCliente(fetchImpl);

    await expect(client.drbac.check("invoice", "approve")).resolves.toBe(true);
    await expect(client.drbac.check("invoice", "read")).resolves.toBe(false);
  });

  it("check() batchea varias llamadas del mismo microtask en un solo request", async () => {
    const { fetchImpl, checkCalls } = makeFakeServer({ "": { version: 1, rules: [] } });
    const client = nuevoCliente(fetchImpl);

    const [a, b] = await Promise.all([
      client.drbac.check("invoice", "approve"),
      client.drbac.check("invoice", "read"),
    ]);

    expect(a).toBe(true);
    expect(b).toBe(false);
    expect(checkCalls).toHaveLength(1);
    expect(checkCalls[0]?.items).toHaveLength(2);
  });

  it("checkMany() manda un solo request con todos los ítems", async () => {
    const { fetchImpl, checkCalls } = makeFakeServer({ "": { version: 1, rules: [] } });
    const client = nuevoCliente(fetchImpl);

    const resultados = await client.drbac.checkMany([
      { resource: "invoice", action: "approve" },
      { resource: "invoice", action: "read" },
    ]);

    expect(resultados).toEqual([true, false]);
    expect(checkCalls).toHaveLength(1);
  });

  it("check() cachea la decisión por un TTL corto, sin volver a pedir", async () => {
    const { fetchImpl, checkCalls } = makeFakeServer({ "": { version: 1, rules: [] } });
    const client = nuevoCliente(fetchImpl);

    await client.drbac.check("invoice", "approve");
    await client.drbac.check("invoice", "approve");

    expect(checkCalls).toHaveLength(1);
  });

  it("check() no usa una decisión cacheada de una versión de scope vieja", async () => {
    const snapshots: Record<string, SnapshotFixture> = { "": { version: 1, rules: [] } };
    const { fetchImpl, checkCalls } = makeFakeServer(snapshots);
    const client = nuevoCliente(fetchImpl);

    await client.drbac.revalidate();
    await client.drbac.check("invoice", "approve");
    expect(checkCalls).toHaveLength(1);

    // La versión del scope subió (una política cambió): la decisión cacheada con la versión
    // vieja no puede reusarse, aunque el TTL de la cache de decisiones todavía no venció.
    snapshots[""] = { version: 2, rules: [] };
    await client.drbac.revalidate();
    await client.drbac.check("invoice", "approve");
    expect(checkCalls).toHaveLength(2);
  });

  it("check() ante un error de red falla cerrando (false), no explota", async () => {
    const { fetchImpl } = createFakeFetch(async (url) => {
      if (new URL(url).pathname.endsWith("/auth/drbac/check")) {
        throw new Error("network down");
      }
      return jsonResponse({
        body: { scope: "", version: 1, expires_at: "2026-01-01T00:10:00.000Z", rules: [] },
      });
    });
    const client = nuevoCliente(fetchImpl);

    await expect(client.drbac.check("invoice", "approve")).resolves.toBe(false);
  });

  it("snapshot()/subscribe() exponen el estado de carga", async () => {
    const { fetchImpl } = makeFakeServer({ "": { version: 1, rules: [] } });
    const client = nuevoCliente(fetchImpl);

    expect(client.drbac.snapshot().status).toBe("loading");

    const listener = vi.fn();
    client.drbac.subscribe(listener);
    await client.drbac.revalidate();

    expect(listener).toHaveBeenCalled();
    expect(client.drbac.snapshot().status).toBe("ready");
  });

  it("requiere el plugin rbac registrado", () => {
    const { fetchImpl } = makeFakeServer({ "": { version: 1, rules: [] } });
    expect(() =>
      createDarwinClient({
        baseUrl: "https://api.test",
        transport: new BearerTransport({ storage: memoryStorage() }),
        fetch: fetchImpl,
        hydrateOnCreate: false,
        plugins: [drbac({ ac })],
      }),
    ).toThrow(/requires "rbac"/);
  });
});

describe("plugin drbac — tipos", () => {
  it("evaluate()/check() sólo aceptan acciones válidas para el recurso dado", () => {
    const { fetchImpl } = makeFakeServer({ "": { version: 1, rules: [] } });
    const client = nuevoCliente(fetchImpl);

    client.drbac.evaluate("invoice", "approve");
    client.drbac.evaluate("invoice", "*");
    // @ts-expect-error — "delete" no es una acción de "invoice", es de "user".
    client.drbac.evaluate("invoice", "delete");
    // @ts-expect-error — "factura" no es un recurso declarado en el schema.
    client.drbac.evaluate("factura", "read");
  });
});
