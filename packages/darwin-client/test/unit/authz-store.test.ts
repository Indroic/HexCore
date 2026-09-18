import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPermissionStore } from "../../src/authz/store";
import type { SessionState } from "../../src/core/types";
import { countCallsTo, createFakeFetch, jsonResponse } from "./helpers/fake-fetch";

function fakeSession(estadoInicial: SessionState) {
  let estado = estadoInicial;
  const listeners = new Set<() => void>();
  return {
    getSnapshot: () => estado,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setState(nuevo: SessionState) {
      estado = nuevo;
      for (const listener of listeners) listener();
    },
  };
}

function permissionsResponse(overrides: Partial<Record<string, unknown>> = {}) {
  return jsonResponse({
    body: {
      scope: "",
      roles: ["admin"],
      permissions: ["invoice.*"],
      version: 1,
      expires_at: "2026-01-01T00:00:10.000Z",
      ...overrides,
    },
  });
}

describe("createPermissionStore", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("getSnapshot()/grants() antes de cargar responde fail-closed sin esperar", async () => {
    const { fetchImpl } = createFakeFetch(async () => permissionsResponse());
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session: fakeSession({ status: "unauthenticated" }),
    });

    const snapshot = store.getSnapshot();
    expect(snapshot.status).toBe("loading");
    expect(store.grants("", "invoice.read")).toBe(false);

    store.dispose();
  });

  it("dispara el fetch en el primer acceso y notifica al resolver", async () => {
    const { fetchImpl, calls } = createFakeFetch(async () => permissionsResponse());
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session: fakeSession({ status: "unauthenticated" }),
    });

    const listener = vi.fn();
    store.subscribe(listener);
    store.getSnapshot();
    await vi.advanceTimersByTimeAsync(0);
    expect(listener).toHaveBeenCalled();

    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(1);
    const snapshot = store.getSnapshot();
    expect(snapshot.status).toBe("ready");
    expect(snapshot.roles).toEqual(["admin"]);
    expect(store.grants("", "invoice.approve")).toBe(true);

    store.dispose();
  });

  it("mantiene scopes separados entre sí", async () => {
    const { fetchImpl } = createFakeFetch(async (url) => {
      const scope = new URL(url).searchParams.get("scope");
      return permissionsResponse({
        scope,
        permissions: scope === "org:1" ? ["invoice.*"] : [],
      });
    });
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session: fakeSession({ status: "unauthenticated" }),
    });

    store.grants("org:1", "invoice.read");
    store.grants("org:2", "invoice.read");
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot("org:1").status).toBe("ready");
    expect(store.getSnapshot("org:2").status).toBe("ready");

    expect(store.grants("org:1", "invoice.read")).toBe(true);
    expect(store.grants("org:2", "invoice.read")).toBe(false);

    store.dispose();
  });

  it("un error de red no pisa un snapshot ready anterior", async () => {
    let falla = false;
    const { fetchImpl } = createFakeFetch(async () => {
      if (falla) throw new Error("network down");
      return permissionsResponse();
    });
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session: fakeSession({ status: "unauthenticated" }),
    });

    await store.revalidate();
    expect(store.getSnapshot().status).toBe("ready");

    falla = true;
    await store.revalidate();
    // Sigue "ready" (no se pisa con un snapshot vacío) — sólo un `notifyAccessDenied` explícito
    // o el vencimiento del TTL lo marcan "stale".
    expect(store.getSnapshot().status).toBe("ready");
    expect(store.grants("", "invoice.read")).toBe(true);

    store.dispose();
  });

  it("notifyAccessDenied() marca el scope stale y revalida en segundo plano", async () => {
    const { fetchImpl, calls } = createFakeFetch(async () => permissionsResponse());
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session: fakeSession({ status: "unauthenticated" }),
    });

    await store.revalidate();
    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(1);

    store.notifyAccessDenied();
    expect(store.getSnapshot().status).toBe("stale");
    await vi.advanceTimersByTimeAsync(0);
    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(2);

    store.dispose();
  });

  it("el TTL vence y marca el scope stale, revalidando solo", async () => {
    const { fetchImpl, calls } = createFakeFetch(async () => permissionsResponse());
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session: fakeSession({ status: "unauthenticated" }),
    });

    await store.revalidate();
    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(1);

    await vi.advanceTimersByTimeAsync(10_000);
    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(2);

    store.dispose();
  });

  it("un sign-in limpia todos los scopes cacheados y refetchea el scope por defecto", async () => {
    const { fetchImpl, calls } = createFakeFetch(async () => permissionsResponse());
    const session = fakeSession({ status: "unauthenticated" });
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session,
    });

    await store.revalidate();
    expect(store.getSnapshot().status).toBe("ready");

    session.setState({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });

    // Se descarta lo cacheado inmediatamente...
    expect(store.getSnapshot().status).toBe("loading");
    // ...y se dispara un refetch del scope por defecto.
    await vi.advanceTimersByTimeAsync(0);
    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(2);

    store.dispose();
  });

  it("un sign-out limpia todos los scopes cacheados sin refetchear", async () => {
    const { fetchImpl, calls } = createFakeFetch(async () => permissionsResponse());
    const session = fakeSession({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session,
    });

    await store.revalidate();
    const llamadasPrevias = countCallsTo(calls, "/auth/rbac/me/permissions?scope=");

    session.setState({ status: "unauthenticated" });

    // El evento de sign-out en sí mismo no dispara ningún fetch.
    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(llamadasPrevias);
    // `getSnapshot()` sí dispara su propio fetch perezoso para el scope, ahora vacío — eso es
    // aparte, y es el contrato de "primer acceso" documentado más arriba, no un efecto del
    // sign-out.
    expect(store.getSnapshot().status).toBe("loading");

    store.dispose();
  });

  it("dehydrate()/hydrate() no disparan ningún fetch", async () => {
    const { fetchImpl, calls } = createFakeFetch(async () => permissionsResponse());
    const store = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session: fakeSession({ status: "unauthenticated" }),
    });

    await store.revalidate();
    const datos = store.dehydrate();
    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(1);

    const store2 = createPermissionStore({
      $fetch: async (path) => fetchImpl(`https://api.test${path}`).then((r) => r.json()),
      session: fakeSession({ status: "unauthenticated" }),
    });
    store2.hydrate(datos);

    expect(store2.getSnapshot().status).toBe("ready");
    expect(store2.grants("", "invoice.read")).toBe(true);
    expect(countCallsTo(calls, "/auth/rbac/me/permissions?scope=")).toBe(1);

    store.dispose();
    store2.dispose();
  });
});
