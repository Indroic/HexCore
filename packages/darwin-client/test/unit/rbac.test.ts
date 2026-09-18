import { afterEach, beforeEach, describe, expect, expectTypeOf, it, vi } from "vitest";
import { defineAccessControl } from "../../src/authz/schema";
import { createDarwinClient } from "../../src/core/client";
import type { RbacApi } from "../../src/plugins/rbac";
import { rbac } from "../../src/plugins/rbac";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { createFakeFetch, jsonResponse } from "./helpers/fake-fetch";

const ac = defineAccessControl({
  resources: { invoice: ["read", "create", "approve"], users: ["invite"] },
  roles: ["viewer", "admin"] as const,
});

function fakeServer(porScope: Record<string, { roles: string[]; permissions: string[] }>) {
  return createFakeFetch(async (url) => {
    const scope = new URL(url).searchParams.get("scope") ?? "";
    const datos = porScope[scope] ?? { roles: [], permissions: [] };
    return jsonResponse({
      body: {
        scope,
        roles: datos.roles,
        permissions: datos.permissions,
        version: 1,
        expires_at: "2026-01-01T00:10:00.000Z",
      },
    });
  });
}

function nuevoCliente(fetchImpl: typeof fetch, scope?: string) {
  return createDarwinClient({
    baseUrl: "https://api.test",
    transport: new BearerTransport({ storage: memoryStorage() }),
    fetch: fetchImpl,
    hydrateOnCreate: false,
    plugins: [rbac({ ac, ...(scope !== undefined ? { scope } : {}) })],
  });
}

describe("plugin rbac", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("can()/hasPermission() responden false fail-closed antes de que el fetch resuelva", () => {
    const { fetchImpl } = fakeServer({
      "": { roles: ["admin"], permissions: ["invoice.*"] },
    });
    const client = nuevoCliente(fetchImpl);

    expect(client.rbac.can("invoice", "approve")).toBe(false);
    expect(client.rbac.hasPermission("invoice.approve")).toBe(false);
  });

  it("can()/hasPermission()/hasRole() reflejan el snapshot una vez resuelto", async () => {
    const { fetchImpl } = fakeServer({
      "": { roles: ["admin"], permissions: ["invoice.*"] },
    });
    const client = nuevoCliente(fetchImpl);

    await client.rbac.revalidate();

    expect(client.rbac.can("invoice", "approve")).toBe(true);
    expect(client.rbac.can("users", "invite")).toBe(false);
    expect(client.rbac.hasPermission("invoice.read")).toBe(true);
    expect(client.rbac.hasRole("admin")).toBe(true);
    expect(client.rbac.hasRole("viewer")).toBe(false);
  });

  it("respeta el scope explícito de una llamada por sobre el scope por defecto del plugin", async () => {
    const { fetchImpl } = fakeServer({
      "": { roles: [], permissions: [] },
      "org:1": { roles: ["admin"], permissions: ["invoice.*"] },
    });
    const client = nuevoCliente(fetchImpl);

    await client.rbac.revalidate({ scope: "org:1" });

    expect(client.rbac.can("invoice", "approve")).toBe(false);
    expect(client.rbac.can("invoice", "approve", { scope: "org:1" })).toBe(true);
  });

  it("usa el scope por defecto declarado en las opciones del plugin", async () => {
    const { fetchImpl, calls } = fakeServer({
      "org:default": { roles: ["viewer"], permissions: ["invoice.read"] },
    });
    const client = nuevoCliente(fetchImpl, "org:default");

    await client.rbac.revalidate();

    expect(calls[0]?.url).toContain("scope=org%3Adefault");
    expect(client.rbac.hasRole("viewer")).toBe(true);
  });

  it("snapshot() expone el estado completo, no sólo un booleano", async () => {
    const { fetchImpl } = fakeServer({
      "": { roles: ["admin"], permissions: ["invoice.*"] },
    });
    const client = nuevoCliente(fetchImpl);

    await client.rbac.revalidate();

    expect(client.rbac.snapshot()).toMatchObject({
      status: "ready",
      roles: ["admin"],
      permissions: ["invoice.*"],
    });
  });

  it("subscribe() notifica cuando el snapshot cambia", async () => {
    const { fetchImpl } = fakeServer({
      "": { roles: ["admin"], permissions: ["invoice.*"] },
    });
    const client = nuevoCliente(fetchImpl);

    const listener = vi.fn();
    client.rbac.subscribe(listener);
    await client.rbac.revalidate();

    expect(listener).toHaveBeenCalled();
  });

  it("notifyAccessDenied() marca el scope stale y revalida en segundo plano", async () => {
    const { fetchImpl, calls } = fakeServer({
      "": { roles: ["admin"], permissions: ["invoice.*"] },
    });
    const client = nuevoCliente(fetchImpl);

    await client.rbac.revalidate();
    const llamadasPrevias = calls.length;

    client.rbac.notifyAccessDenied();
    expect(client.rbac.snapshot().status).toBe("stale");

    await vi.advanceTimersByTimeAsync(0);
    expect(calls.length).toBeGreaterThan(llamadasPrevias);
  });

  it("dehydrate()/hydrate() repueblan otra instancia sin fetchear", async () => {
    const { fetchImpl, calls } = fakeServer({
      "": { roles: ["admin"], permissions: ["invoice.*"] },
    });
    const client = nuevoCliente(fetchImpl);
    await client.rbac.revalidate();

    const datos = client.rbac.dehydrate();
    const llamadasPrevias = calls.length;

    const client2 = nuevoCliente(fetchImpl);
    client2.rbac.hydrate(datos);

    expect(client2.rbac.can("invoice", "approve")).toBe(true);
    expect(calls.length).toBe(llamadasPrevias);
  });
});

describe("plugin rbac — tipos", () => {
  it("client.rbac tiene el tipo de RbacApi para el schema declarado", () => {
    const client = createDarwinClient({
      baseUrl: "https://api.test",
      transport: new BearerTransport({ storage: memoryStorage() }),
      hydrateOnCreate: false,
      plugins: [rbac({ ac })],
    });

    expectTypeOf(client.rbac).toEqualTypeOf<
      RbacApi<typeof ac.resources, "viewer" | "admin">
    >();
  });

  it("can() sólo acepta acciones válidas para el recurso dado", () => {
    const client = createDarwinClient({
      baseUrl: "https://api.test",
      transport: new BearerTransport({ storage: memoryStorage() }),
      hydrateOnCreate: false,
      plugins: [rbac({ ac })],
    });

    client.rbac.can("invoice", "approve");
    client.rbac.can("invoice", "*");
    // @ts-expect-error — "invite" no es una acción de "invoice", es de "users".
    client.rbac.can("invoice", "invite");
    // @ts-expect-error — "facturas" no es un recurso declarado en el schema.
    client.rbac.can("facturas", "read");
  });

  it("hasRole() sólo acepta roles declarados en el schema", () => {
    const client = createDarwinClient({
      baseUrl: "https://api.test",
      transport: new BearerTransport({ storage: memoryStorage() }),
      hydrateOnCreate: false,
      plugins: [rbac({ ac })],
    });

    client.rbac.hasRole("admin");
    // @ts-expect-error — "superadmin" no está en `roles` del `AccessControl`.
    client.rbac.hasRole("superadmin");
  });
});
