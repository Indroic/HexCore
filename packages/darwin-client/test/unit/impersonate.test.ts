import { describe, expect, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { impersonate } from "../../src/plugins/impersonate";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { jsonResponse } from "./helpers/fake-fetch";

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fakeServer() {
  let currentAccessToken = "";
  let impersonando = false;

  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = new Headers(init?.headers);
    const autenticado =
      currentAccessToken !== "" &&
      headers.get("Authorization") === `Bearer ${currentAccessToken}`;

    if (path === "/auth/sign-in" && method === "POST") {
      currentAccessToken = "at-operador";
      return jsonResponse({
        body: {
          session_id: "s-operador",
          expires_in: 120,
          token_type: "Bearer",
          access_token: currentAccessToken,
          refresh_token: "rt-operador",
        },
      });
    }

    if (path === "/auth/impersonate" && method === "GET") {
      if (!autenticado) {
        return jsonResponse({
          status: 401,
          body: { detail: "sin credencial", error: "UnauthenticatedError" },
        });
      }
      return jsonResponse({
        body: impersonando
          ? {
              active: true,
              actor_id: "operador-1",
              subject_id: "sujeto-1",
              reason: "soporte",
              expires_at: "2026-01-01T00:10:00Z",
            }
          : {
              active: false,
              actor_id: null,
              subject_id: null,
              reason: null,
              expires_at: null,
            },
      });
    }

    if (path === "/auth/impersonate/sujeto-1" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { reason: string };
      if (!body.reason) {
        return jsonResponse({
          status: 422,
          body: { detail: "reason requerido", error: "ValidationError" },
        });
      }
      currentAccessToken = "at-impersonado";
      impersonando = true;
      return jsonResponse({
        body: {
          session_id: "s-impersonada",
          expires_in: 120,
          token_type: "Bearer",
          access_token: currentAccessToken,
          refresh_token: "rt-impersonado",
          impersonating: "sujeto-1",
          subject_id: "sujeto-1",
          expires_at: "2026-01-01T00:10:00Z",
        },
      });
    }

    if (path === "/auth/impersonate/stop" && method === "POST") {
      impersonando = false;
      return jsonResponse({ body: { stopped: true } });
    }

    if (path === "/auth/me" && method === "GET") {
      if (!autenticado) {
        return jsonResponse({
          status: 401,
          body: { detail: "sin credencial", error: "UnauthenticatedError" },
        });
      }
      return jsonResponse({
        body: impersonando
          ? { actor_id: "operador-1", subject_id: "sujeto-1", impersonating: true }
          : { actor_id: "operador-1", subject_id: "operador-1", impersonating: false },
      });
    }

    throw new Error(`ruta no simulada: ${method} ${path}`);
  }) as typeof fetch;

  return { fetchImpl };
}

async function nuevoClienteComoOperador(fetchImpl: typeof fetch) {
  const client = createDarwinClient({
    baseUrl: "https://api.test",
    transport: new BearerTransport({ storage: memoryStorage() }),
    fetch: fetchImpl,
    hydrateOnCreate: false,
    plugins: [impersonate()],
  });
  await client.signIn("operador@test.com", "loquesea");
  return client;
}

describe("plugin impersonate", () => {
  it("status() sin impersonar da active: false", async () => {
    const { fetchImpl } = fakeServer();
    const client = await nuevoClienteComoOperador(fetchImpl);

    await expect(client.impersonate.status()).resolves.toEqual({
      active: false,
      actorId: null,
      subjectId: null,
      reason: null,
      expiresAt: null,
    });
  });

  it("start() sin reason falla (422 del borde)", async () => {
    const { fetchImpl } = fakeServer();
    const client = await nuevoClienteComoOperador(fetchImpl);

    await expect(client.impersonate.start("sujeto-1", "")).rejects.toBeTruthy();
  });

  it("start() completa el login como el sujeto: el store queda authenticated con esa sesión", async () => {
    const { fetchImpl } = fakeServer();
    const client = await nuevoClienteComoOperador(fetchImpl);

    const resultado = await client.impersonate.start("sujeto-1", "verificar un bug");

    expect(resultado.status).toBe("signed-in");
    await tick();
    expect(client.session.getSnapshot()).toEqual({
      status: "authenticated",
      me: { actor_id: "operador-1", subject_id: "sujeto-1", impersonating: true },
    });

    await expect(client.impersonate.status()).resolves.toMatchObject({
      active: true,
      subjectId: "sujeto-1",
    });
  });

  it("stop() no toca el store: la app decide qué hacer después", async () => {
    const { fetchImpl } = fakeServer();
    const client = await nuevoClienteComoOperador(fetchImpl);

    await client.impersonate.start("sujeto-1", "verificar un bug");
    await tick();

    await expect(client.impersonate.stop()).resolves.toEqual({ stopped: true });
    expect(client.session.getSnapshot()).toEqual({
      status: "authenticated",
      me: { actor_id: "operador-1", subject_id: "sujeto-1", impersonating: true },
    });
  });
});
