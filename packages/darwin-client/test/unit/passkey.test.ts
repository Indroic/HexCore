import { describe, expect, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { passkey } from "../../src/plugins/passkey";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { jsonResponse } from "./helpers/fake-fetch";

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

const RESUMEN_SERVIDOR = {
  id: "p1",
  name: "mi laptop",
  aaguid: null,
  backed_up: true,
  created_at: "2026-01-01T00:00:00Z",
  last_used_at: null,
};

function fakeServer() {
  let currentAccessToken = "";
  let passkeys = [RESUMEN_SERVIDOR];

  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const method = (init?.method ?? "GET").toUpperCase();

    if (path === "/auth/passkey/register/options" && method === "POST") {
      return jsonResponse({ body: { challenge: "c1", rp: { name: "hexcore" } } });
    }

    if (path === "/auth/passkey/register" && method === "POST") {
      return jsonResponse({ status: 201, body: RESUMEN_SERVIDOR });
    }

    if (path === "/auth/passkey/authenticate/options" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { email: string | null };
      // Sólo un email que de verdad tiene cuenta angosta las opciones — un mail desconocido
      // trae la misma forma que el flujo sin mail, y es justo lo que este test verifica.
      return jsonResponse({
        body:
          body.email === "conocido@test.com"
            ? { challenge: "c2", allowCredentials: [] }
            : { challenge: "c2" },
      });
    }

    if (path === "/auth/passkey/authenticate" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { credential: { id: string } };
      if (body.credential.id !== "cred-valida") {
        return jsonResponse({
          status: 401,
          body: { detail: "aserción inválida", error: "InvalidCredentialsError" },
        });
      }
      currentAccessToken = "at-tras-passkey";
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

    if (path === "/auth/passkey" && method === "GET") {
      return jsonResponse({ body: passkeys });
    }

    if (path === "/auth/passkey/p1" && method === "DELETE") {
      passkeys = [];
      return jsonResponse({ body: { deleted: true } });
    }

    if (path === "/auth/me" && method === "GET") {
      const headers = new Headers(init?.headers);
      if (
        currentAccessToken &&
        headers.get("Authorization") === `Bearer ${currentAccessToken}`
      ) {
        return jsonResponse({
          body: { actor_id: "u1", subject_id: "u1", impersonating: false },
        });
      }
      return jsonResponse({
        status: 401,
        body: { detail: "sin credencial", error: "UnauthenticatedError" },
      });
    }

    throw new Error(`ruta no simulada: ${method} ${path}`);
  }) as typeof fetch;

  return { fetchImpl };
}

function nuevoCliente(fetchImpl: typeof fetch) {
  return createDarwinClient({
    baseUrl: "https://api.test",
    transport: new BearerTransport({ storage: memoryStorage() }),
    fetch: fetchImpl,
    hydrateOnCreate: false,
    plugins: [passkey()],
  });
}

describe("plugin passkey", () => {
  it("registerOptions() y register() delegan en las rutas correspondientes", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.passkey.registerOptions()).resolves.toMatchObject({
      challenge: "c1",
    });

    const resumen = await client.passkey.register({ id: "cred-1" }, "mi laptop");
    expect(resumen).toEqual({
      id: "p1",
      name: "mi laptop",
      aaguid: null,
      backedUp: true,
      createdAt: "2026-01-01T00:00:00Z",
      lastUsedAt: null,
    });
  });

  it("authenticateOptions() no revela si la cuenta existe (misma forma con o sin email)", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const desconocido = await client.passkey.authenticateOptions("no-existe@test.com");
    const sinEmail = await client.passkey.authenticateOptions();
    expect(desconocido).toEqual(sinEmail);
  });

  it("authenticate() con una credencial válida completa el login", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const resultado = await client.passkey.authenticate({ id: "cred-valida" });

    expect(resultado.status).toBe("signed-in");
    await tick();
    expect(client.session.getSnapshot()).toEqual({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });
  });

  it("authenticate() con una credencial inválida lanza y no toca el store", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.passkey.authenticate({ id: "cred-mala" })).rejects.toMatchObject({
      code: "InvalidCredentialsError",
    });
    expect(client.session.getSnapshot()).toEqual({ status: "unauthenticated" });
  });

  it("list() y remove() delegan en las rutas correspondientes", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.passkey.list()).resolves.toEqual([
      {
        id: "p1",
        name: "mi laptop",
        aaguid: null,
        backedUp: true,
        createdAt: "2026-01-01T00:00:00Z",
        lastUsedAt: null,
      },
    ]);

    await expect(client.passkey.remove("p1")).resolves.toEqual({ deleted: true });
    await expect(client.passkey.list()).resolves.toEqual([]);
  });
});
