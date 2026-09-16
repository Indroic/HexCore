import { describe, expect, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { twoFactor } from "../../src/plugins/two-factor";
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

    if (path === "/auth/2fa" && method === "GET") {
      return jsonResponse({ body: { enrolled: true, confirmed: true } });
    }

    if (path === "/auth/2fa/enroll" && method === "POST") {
      return jsonResponse({
        body: { secret: "SECRETO", uri: "otpauth://totp/x", confirmed: false },
      });
    }

    if (path === "/auth/2fa/confirm" && method === "POST") {
      return jsonResponse({ body: { confirmed: true } });
    }

    if (path === "/auth/2fa/disable" && method === "POST") {
      return jsonResponse({ body: { disabled: true } });
    }

    if (path === "/auth/2fa/challenge" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { challenge: string; code: string };
      if (body.challenge !== "desafio-valido" || body.code !== "123456") {
        return jsonResponse({
          status: 401,
          body: { detail: "código incorrecto", error: "TwoFactorInvalidCodeError" },
        });
      }
      currentAccessToken = "at-tras-2fa";
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
    plugins: [twoFactor()],
  });
}

describe("plugin twoFactor", () => {
  it("status() refleja el estado del servidor", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.twoFactor.status()).resolves.toEqual({
      enrolled: true,
      confirmed: true,
    });
  });

  it("enroll() devuelve el secreto y la uri", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const inscripcion = await client.twoFactor.enroll();
    expect(inscripcion.secret).toBe("SECRETO");
    expect(inscripcion.confirmed).toBe(false);
  });

  it("confirm() y disable() delegan en las rutas correspondientes", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.twoFactor.confirm("123456")).resolves.toEqual({ confirmed: true });
    await expect(client.twoFactor.disable("123456")).resolves.toEqual({ disabled: true });
  });

  it("complete() con el challenge correcto completa el login: el store queda authenticated", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const resultado = await client.twoFactor.complete("desafio-valido", "123456");

    expect(resultado.status).toBe("signed-in");
    await tick();
    expect(client.session.getSnapshot()).toEqual({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });
  });

  it("complete() con un código incorrecto lanza y no toca el store", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(
      client.twoFactor.complete("desafio-valido", "000000"),
    ).rejects.toMatchObject({
      code: "TwoFactorInvalidCodeError",
    });
    expect(client.session.getSnapshot()).toEqual({ status: "unauthenticated" });
  });
});
