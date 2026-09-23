import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSession } from "../../src/session/session";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { darwinErrorResponse, jsonResponse } from "./helpers/fake-fetch";

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fakeServer(options: { twoFactor?: boolean } = {}) {
  let currentAccessToken = "";
  let refreshCount = 0;
  let signInCount = 0;
  let meCount = 0;

  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const method = (init?.method ?? "GET").toUpperCase();

    if (path === "/auth/sign-in" && method === "POST") {
      signInCount += 1;
      if (options.twoFactor) {
        return darwinErrorResponse(401, "TwoFactorRequiredError", {
          challenge: "desafio-1",
        });
      }
      currentAccessToken = `at-${signInCount}`;
      return jsonResponse({
        body: {
          session_id: "s1",
          expires_in: 120,
          token_type: "Bearer",
          access_token: currentAccessToken,
          refresh_token: "rt-1",
        },
      });
    }

    if (path === "/auth/refresh" && method === "POST") {
      refreshCount += 1;
      currentAccessToken = `at-refreshed-${refreshCount}`;
      return jsonResponse({
        body: {
          session_id: "s1",
          expires_in: 120,
          token_type: "Bearer",
          access_token: currentAccessToken,
          refresh_token: "rt-1",
        },
      });
    }

    if (path === "/auth/sign-out" && method === "POST") {
      return jsonResponse({ body: { signed_out: true } });
    }

    if (path === "/auth/me" && method === "GET") {
      meCount += 1;
      const headers = new Headers(init?.headers);
      if (
        currentAccessToken &&
        headers.get("Authorization") === `Bearer ${currentAccessToken}`
      ) {
        return jsonResponse({
          body: { actor_id: "u1", subject_id: "u1", impersonating: false },
        });
      }
      return darwinErrorResponse(401, "TokenExpiredError");
    }

    throw new Error(`ruta no simulada: ${method} ${path}`);
  }) as typeof fetch;

  return {
    fetchImpl,
    getRefreshCount: () => refreshCount,
    getMeCount: () => meCount,
  };
}

function nuevaSesion(
  fetchImpl: typeof fetch,
  extra: Partial<Parameters<typeof createSession>[0]> = {},
) {
  return createSession({
    baseUrl: "https://api.test",
    transport: new BearerTransport({ storage: memoryStorage() }),
    fetch: fetchImpl,
    hydrateOnCreate: false,
    ...extra,
  });
}

describe("createSession", () => {
  it("signIn exitoso deja el store en authenticated tras hidratar", async () => {
    const { fetchImpl } = fakeServer();
    const session = nuevaSesion(fetchImpl);

    const resultado = await session.signIn("a@a.com", "pass");
    expect(resultado.status).toBe("signed-in");

    await tick();
    expect(session.getSnapshot()).toEqual({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });
  });

  it("signIn manda el identificador como 'identifier', no como 'email'", async () => {
    // El backend acepta los tres nombres para no romper a un front de 9.x, pero el cliente
    // manda el canónico: si mandara 'email', un identificador que es un nombre de usuario
    // viajaría en un campo que dice ser otra cosa.
    let cuerpo: unknown;
    const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
      const path = new URL(String(url)).pathname;
      if (path === "/auth/sign-in") {
        cuerpo = JSON.parse(String(init?.body));
        return jsonResponse({
          body: {
            session_id: "s1",
            expires_in: 120,
            token_type: "Bearer",
            access_token: "at",
          },
        });
      }
      return jsonResponse({
        body: { actor_id: "u1", subject_id: "u1", impersonating: false },
      });
    }) as unknown as typeof fetch;

    const session = nuevaSesion(fetchImpl);
    await session.signIn("indroic", "pass");

    expect(cuerpo).toEqual({ identifier: "indroic", password: "pass" });
  });

  it("signIn con 2FA activado devuelve el resultado discriminado, sin tocar el store", async () => {
    const { fetchImpl } = fakeServer({ twoFactor: true });
    const session = nuevaSesion(fetchImpl);

    const resultado = await session.signIn("a@a.com", "pass");

    expect(resultado).toEqual({ status: "two-factor-required", challenge: "desafio-1" });
    expect(session.getSnapshot()).toEqual({ status: "unauthenticated" });
  });

  it("N refresh() concurrentes disparan un solo POST /auth/refresh (single-flight)", async () => {
    const { fetchImpl, getRefreshCount } = fakeServer();
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");

    const N = 10;
    await Promise.all(Array.from({ length: N }, () => session.refresh()));

    expect(getRefreshCount()).toBe(1);
  });

  it("signOut limpia el storage y deja el store unauthenticated con reason signed-out", async () => {
    const { fetchImpl } = fakeServer();
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");
    await tick();

    await session.signOut();

    expect(session.getSnapshot()).toEqual({
      status: "unauthenticated",
      reason: "signed-out",
    });
  });

  it("me() deduplica dos llamadas concurrentes en un solo request", async () => {
    let resueltas = 0;
    let llamadasAMe = 0;
    const fetchImpl = (async (url: string | URL) => {
      const path = new URL(String(url)).pathname;
      if (path === "/auth/me") {
        llamadasAMe += 1;
        await new Promise((r) => setTimeout(r, 5));
        resueltas += 1;
        return jsonResponse({
          body: { actor_id: "u1", subject_id: "u1", impersonating: false },
        });
      }
      throw new Error(`ruta no simulada: ${path}`);
    }) as typeof fetch;

    const session = nuevaSesion(fetchImpl);

    const [a, b] = await Promise.all([session.me(), session.me()]);

    expect(llamadasAMe).toBe(1);
    expect(resueltas).toBe(1);
    expect(a).toEqual(b);
  });

  it("getSnapshot es referencialmente estable entre llamadas sin cambios de estado", () => {
    const { fetchImpl } = fakeServer();
    const session = nuevaSesion(fetchImpl);

    expect(session.getSnapshot()).toBe(session.getSnapshot());
  });

  it("hydrateOnCreate: false arranca en unauthenticated y no en loading", () => {
    const { fetchImpl } = fakeServer();
    const session = nuevaSesion(fetchImpl);

    expect(session.getSnapshot()).toEqual({ status: "unauthenticated" });
  });

  it("getServerSnapshot no toca globals y siempre da unauthenticated", () => {
    const { fetchImpl } = fakeServer();
    const session = nuevaSesion(fetchImpl);

    expect(session.getServerSnapshot()).toEqual({ status: "unauthenticated" });
  });
});

/**
 * Regresión del incidente: dejar la pestaña abierta sin interactuar cerraba la sesión en vez
 * de refrescarla sola. Cubre las tres piezas del fix: (1) un fallo transitorio del refresh no
 * mata la sesión, (2) hay un timer de fondo que refresca sin que la app haga ningún `$fetch`,
 * (3) `visibilitychange`/`online` son el atajo para cuando el navegador throttleó ese timer.
 */
describe("createSession — refresco de fondo", () => {
  const sesionInicial = {
    session_id: "s1",
    expires_in: 120,
    token_type: "Bearer" as const,
    access_token: "at-1",
    refresh_token: "rt-1",
  };
  const meBody = { actor_id: "u1", subject_id: "u1", impersonating: false };

  function fetchImplCon(
    manejarRefresh: (intento: number) => Response | Promise<Response>,
  ): { fetchImpl: typeof fetch; getRefreshCount: () => number } {
    let refreshCount = 0;
    const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
      const path = new URL(String(url)).pathname;
      const method = (init?.method ?? "GET").toUpperCase();

      if (path === "/auth/sign-in" && method === "POST") {
        return jsonResponse({ body: sesionInicial });
      }
      if (path === "/auth/me" && method === "GET") {
        return jsonResponse({ body: meBody });
      }
      if (path === "/auth/refresh" && method === "POST") {
        refreshCount += 1;
        return manejarRefresh(refreshCount);
      }
      throw new Error(`ruta no simulada: ${method} ${path}`);
    }) as typeof fetch;

    return { fetchImpl, getRefreshCount: () => refreshCount };
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("un refresh con veredicto de reuso (TokenRevokedError) cierra la sesión con reason revoked", async () => {
    const { fetchImpl } = fetchImplCon(() => darwinErrorResponse(401, "TokenRevokedError"));
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");

    await expect(session.refresh()).rejects.toThrow();

    expect(session.getSnapshot()).toEqual({ status: "unauthenticated", reason: "revoked" });
    session.dispose();
  });

  it("un refresh con veredicto de vencimiento (TokenExpiredError) cierra la sesión con reason refresh-failed", async () => {
    const { fetchImpl } = fetchImplCon(() => darwinErrorResponse(401, "TokenExpiredError"));
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");

    await expect(session.refresh()).rejects.toThrow();

    expect(session.getSnapshot()).toEqual({
      status: "unauthenticated",
      reason: "refresh-failed",
    });
    session.dispose();
  });

  it("un refresh que falla por un error de red NO cierra la sesión: sigue authenticated", async () => {
    const { fetchImpl, getRefreshCount } = fetchImplCon(() => {
      throw new TypeError("la red se cayó");
    });
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");

    await expect(session.refresh()).rejects.toThrow();

    expect(session.getSnapshot().status).toBe("authenticated");
    expect(getRefreshCount()).toBe(1);
    session.dispose();
  });

  it("refresca sola en segundo plano, sin ningún $fetch de la app, antes de que venza el access", async () => {
    const { fetchImpl, getRefreshCount } = fetchImplCon(() =>
      jsonResponse({ body: sesionInicial }),
    );
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");
    expect(getRefreshCount()).toBe(0);

    // access_ttl=120s, margen=5s: el timer de fondo dispara a los 115s exactos.
    await vi.advanceTimersByTimeAsync(115_000);

    expect(getRefreshCount()).toBe(1);
    session.dispose();
  });

  it("tras un fallo transitorio del timer de fondo, reintenta en corto en vez de esperar el próximo ciclo", async () => {
    const { fetchImpl, getRefreshCount } = fetchImplCon((intento) => {
      if (intento === 1) throw new TypeError("la red se cayó");
      return jsonResponse({ body: sesionInicial });
    });
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");

    await vi.advanceTimersByTimeAsync(115_000);
    expect(getRefreshCount()).toBe(1);
    expect(session.getSnapshot().status).toBe("authenticated");

    // El reintento corto (5s), no otro ciclo completo de 115s.
    await vi.advanceTimersByTimeAsync(5_000);
    expect(getRefreshCount()).toBe(2);
    session.dispose();
  });

  it("visibilitychange a 'visible' con el access ya vencido dispara un refresh, aunque el timer de fondo no haya corrido todavía", async () => {
    const listeners = new Map<string, Set<() => void>>();
    function objetoConEventos() {
      return {
        addEventListener: (tipo: string, cb: () => void) => {
          if (!listeners.has(tipo)) listeners.set(tipo, new Set());
          listeners.get(tipo)?.add(cb);
        },
        removeEventListener: (tipo: string, cb: () => void) => {
          listeners.get(tipo)?.delete(cb);
        },
      };
    }
    const doc = { visibilityState: "visible", ...objetoConEventos() };
    vi.stubGlobal("document", doc);
    vi.stubGlobal("window", objetoConEventos());

    const { fetchImpl, getRefreshCount } = fetchImplCon(() =>
      jsonResponse({ body: sesionInicial }),
    );
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");

    // Salta el reloj sin dejar correr los timers pendientes: simula un `setTimeout` que el
    // navegador throttleó por tener la pestaña oculta — el tiempo real ya pasó, pero el timer
    // de fondo todavía no disparó.
    vi.setSystemTime(Date.now() + 116_000);
    expect(getRefreshCount()).toBe(0);

    for (const cb of listeners.get("visibilitychange") ?? []) cb();
    await vi.advanceTimersByTimeAsync(0);

    expect(getRefreshCount()).toBe(1);
    session.dispose();
  });

  it("dispose() da de baja el timer de fondo: nada refresca después", async () => {
    const { fetchImpl, getRefreshCount } = fetchImplCon(() =>
      jsonResponse({ body: sesionInicial }),
    );
    const session = nuevaSesion(fetchImpl);
    await session.signIn("a@a.com", "pass");

    session.dispose();
    await vi.advanceTimersByTimeAsync(200_000);

    expect(getRefreshCount()).toBe(0);
  });
});
