import { describe, expect, it } from "vitest";
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
