import { describe, expect, expectTypeOf, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { impersonate } from "../../src/plugins/impersonate";
import { magicLink } from "../../src/plugins/magic-link";
import { oauth } from "../../src/plugins/oauth";
import { organization } from "../../src/plugins/organization";
import type { PasskeyApi } from "../../src/plugins/passkey";
import { passkey } from "../../src/plugins/passkey";
import type { TwoFactorApi } from "../../src/plugins/two-factor";
import { twoFactor } from "../../src/plugins/two-factor";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { jsonResponse } from "./helpers/fake-fetch";

/**
 * El gate de integración de la Fase 2: no un plugin a la vez (eso ya lo cubre cada
 * `*.test.ts` propio), sino los seis registrados juntos en un solo cliente — que es como
 * los va a usar cualquier app real. Lo que esto tiene que probar y que ningún test unitario
 * puede: que el registro conjunto no colisiona (ids, tipos) y que el estado de sesión que
 * un plugin deja (tokens, store) es el que otro plugin distinto encuentra al arrancar.
 */

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fakeServer() {
  let currentAccessToken = "";

  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = new Headers(init?.headers);
    const autenticado =
      currentAccessToken !== "" &&
      headers.get("Authorization") === `Bearer ${currentAccessToken}`;

    if (path === "/auth/sign-in" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { email: string; password: string };
      if (body.password !== "correcta") {
        return jsonResponse({
          status: 401,
          body: { detail: "credenciales inválidas", error: "InvalidCredentialsError" },
        });
      }
      currentAccessToken = "at-1";
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

    if (path === "/auth/2fa" && method === "GET") {
      if (!autenticado) {
        return jsonResponse({
          status: 401,
          body: { detail: "sin credencial", error: "UnauthenticatedError" },
        });
      }
      return jsonResponse({ body: { enrolled: false, confirmed: false } });
    }

    if (path === "/auth/impersonate" && method === "GET") {
      if (!autenticado) {
        return jsonResponse({
          status: 401,
          body: { detail: "sin credencial", error: "UnauthenticatedError" },
        });
      }
      return jsonResponse({
        body: {
          active: false,
          actor_id: null,
          subject_id: null,
          reason: null,
          expires_at: null,
        },
      });
    }

    if (path === "/organizations" && method === "GET") {
      if (!autenticado) {
        return jsonResponse({
          status: 401,
          body: { detail: "sin credencial", error: "UnauthenticatedError" },
        });
      }
      return jsonResponse({
        body: [{ user_id: "u1", role: "owner", created_at: "2026-01-01T00:00:00Z" }],
      });
    }

    if (path === "/auth/oauth/providers" && method === "GET") {
      return jsonResponse({ body: { providers: ["google"] } });
    }

    if (path === "/auth/magic-link/request" && method === "POST") {
      return jsonResponse({ body: { sent: true } });
    }

    if (path === "/auth/passkey" && method === "GET") {
      if (!autenticado) {
        return jsonResponse({
          status: 401,
          body: { detail: "sin credencial", error: "UnauthenticatedError" },
        });
      }
      return jsonResponse({ body: [] });
    }

    if (path === "/auth/me" && method === "GET") {
      if (!autenticado) {
        return jsonResponse({
          status: 401,
          body: { detail: "sin credencial", error: "UnauthenticatedError" },
        });
      }
      return jsonResponse({
        body: { actor_id: "u1", subject_id: "u1", impersonating: false },
      });
    }

    throw new Error(`ruta no simulada: ${method} ${path}`);
  }) as typeof fetch;

  return { fetchImpl };
}

function nuevoClienteCompleto(fetchImpl: typeof fetch) {
  return createDarwinClient({
    baseUrl: "https://api.test",
    transport: new BearerTransport({ storage: memoryStorage() }),
    fetch: fetchImpl,
    hydrateOnCreate: false,
    plugins: [twoFactor(), magicLink(), oauth(), passkey(), impersonate(), organization()],
  });
}

describe("createDarwinClient con los seis plugins registrados juntos", () => {
  it("valida el registro sin colisiones de id ni ciclos", () => {
    expect(() => nuevoClienteCompleto(fakeServer().fetchImpl)).not.toThrow();
  });

  it("cada plugin cuelga de su propia clave, con el tipo correcto — ninguno tapa al núcleo", () => {
    const client = nuevoClienteCompleto(fakeServer().fetchImpl);

    expectTypeOf(client.twoFactor).toEqualTypeOf<TwoFactorApi>();
    expectTypeOf(client.passkey).toEqualTypeOf<PasskeyApi>();
    expectTypeOf(client.signIn).toBeFunction();
    expectTypeOf(client.session.getSnapshot).toBeFunction();

    expect(Object.keys(client)).toEqual(
      expect.arrayContaining([
        "twoFactor",
        "magicLink",
        "oauth",
        "passkey",
        "impersonate",
        "organization",
        "signIn",
        "signOut",
        "session",
        "refresh",
        "me",
        "$fetch",
      ]),
    );
  });

  it("un signIn() del núcleo deja la sesión que los seis plugins ven — sin volver a autenticar", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoClienteCompleto(fetchImpl);

    const resultado = await client.signIn("actor@test.com", "correcta");
    expect(resultado.status).toBe("signed-in");
    await tick();
    expect(client.session.getSnapshot()).toEqual({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });

    // Cada plugin que exige sesión pega contra el mismo token que dejó signIn() — nadie
    // vuelve a autenticar por su cuenta ni pide un token propio.
    await expect(client.twoFactor.status()).resolves.toEqual({
      enrolled: false,
      confirmed: false,
    });
    await expect(client.impersonate.status()).resolves.toMatchObject({ active: false });
    await expect(client.organization.mine()).resolves.toEqual([
      { userId: "u1", role: "owner", createdAt: "2026-01-01T00:00:00Z" },
    ]);
    await expect(client.passkey.list()).resolves.toEqual([]);

    // Las rutas públicas de otros plugins (que no dependen de la sesión) también funcionan
    // en el mismo cliente, sin que registrarlas todas junto con las autenticadas interfiera.
    await expect(client.oauth.providers()).resolves.toEqual({ providers: ["google"] });
    await expect(client.magicLink.request("nuevo@test.com")).resolves.toEqual({
      sent: true,
    });
  });

  it("sin sesión, las rutas que la exigen fallan con 401 — ningún plugin la asume implícita", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoClienteCompleto(fetchImpl);

    await expect(client.twoFactor.status()).rejects.toMatchObject({
      code: "UnauthenticatedError",
    });
    await expect(client.organization.mine()).rejects.toMatchObject({
      code: "UnauthenticatedError",
    });
  });
});
