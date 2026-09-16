import { describe, expect, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { magicLink } from "../../src/plugins/magic-link";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { jsonResponse } from "./helpers/fake-fetch";

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fakeServer() {
  let currentAccessToken = "";

  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const method = (init?.method ?? "GET").toUpperCase();

    if (path === "/auth/magic-link/request" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { email: string };
      return jsonResponse({
        body:
          body.email === "existe@test.com"
            ? { sent: true, token: "token-de-un-uso" }
            : { sent: true },
      });
    }

    if (path === "/auth/magic-link/consume" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { email: string; token: string };
      if (body.token !== "token-de-un-uso") {
        return jsonResponse({
          status: 401,
          body: { detail: "link inválido o vencido", error: "InvalidCredentialsError" },
        });
      }
      currentAccessToken = "at-tras-magic-link";
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
    plugins: [magicLink()],
  });
}

describe("plugin magicLink", () => {
  it("request() con una cuenta existente trae el token (modo sin mail real)", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.magicLink.request("existe@test.com")).resolves.toEqual({
      sent: true,
      token: "token-de-un-uso",
    });
  });

  it("request() responde igual (sent: true, sin token) para una cuenta que no existe", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.magicLink.request("no-existe@test.com")).resolves.toEqual({
      sent: true,
    });
  });

  it("consume() con un token válido completa el login: el store queda authenticated", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const resultado = await client.magicLink.consume("existe@test.com", "token-de-un-uso");

    expect(resultado.status).toBe("signed-in");
    await tick();
    expect(client.session.getSnapshot()).toEqual({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });
  });

  it("consume() con un token inválido lanza y no toca el store", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(
      client.magicLink.consume("existe@test.com", "token-invalido"),
    ).rejects.toMatchObject({
      code: "InvalidCredentialsError",
    });
    expect(client.session.getSnapshot()).toEqual({ status: "unauthenticated" });
  });
});
