import { describe, expect, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { oauth } from "../../src/plugins/oauth";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { jsonResponse } from "./helpers/fake-fetch";

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fakeServer() {
  let currentAccessToken = "";
  let linked: string[] = ["google"];

  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const parsed = new URL(String(url));
    const path = parsed.pathname;
    const method = (init?.method ?? "GET").toUpperCase();

    if (path === "/auth/oauth/providers" && method === "GET") {
      return jsonResponse({ body: { providers: ["google", "github"] } });
    }

    if (path === "/auth/oauth/google/start" && method === "GET") {
      expect(parsed.searchParams.get("redirect_uri")).toBe("https://app.test/callback");
      return jsonResponse({ body: { url: "https://google.test/consent", state: "st-1" } });
    }

    if (path === "/auth/oauth/google/callback" && method === "GET") {
      if (parsed.searchParams.get("code") !== "codigo-valido") {
        return jsonResponse({
          status: 401,
          body: { detail: "code inválido", error: "InvalidCredentialsError" },
        });
      }
      currentAccessToken = "at-tras-oauth";
      return jsonResponse({
        body: {
          session_id: "s1",
          expires_in: 120,
          token_type: "Bearer",
          access_token: currentAccessToken,
          refresh_token: "rt-1",
          created: true,
        },
      });
    }

    if (path === "/auth/oauth/google/link" && method === "GET") {
      return jsonResponse({ body: { url: "https://google.test/link", state: "st-2" } });
    }

    if (path === "/auth/oauth/linked" && method === "GET") {
      return jsonResponse({ body: { providers: linked } });
    }

    if (path === "/auth/oauth/google" && method === "DELETE") {
      linked = linked.filter((p) => p !== "google");
      return jsonResponse({ body: { unlinked: true } });
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
    plugins: [oauth()],
  });
}

describe("plugin oauth", () => {
  it("providers() lista los proveedores configurados", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.oauth.providers()).resolves.toEqual({
      providers: ["google", "github"],
    });
  });

  it("start() manda redirect_uri en la query y trae url + state", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(
      client.oauth.start("google", "https://app.test/callback"),
    ).resolves.toEqual({ url: "https://google.test/consent", state: "st-1" });
  });

  it("handleCallback() con un code válido completa el login y separa created", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const { result, created } = await client.oauth.handleCallback("google", {
      code: "codigo-valido",
      state: "st-1",
      redirectUri: "https://app.test/callback",
    });

    expect(created).toBe(true);
    expect(result.status).toBe("signed-in");
    await tick();
    expect(client.session.getSnapshot()).toEqual({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });
  });

  it("handleCallback() con un code inválido lanza y no toca el store", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(
      client.oauth.handleCallback("google", {
        code: "codigo-malo",
        state: "st-1",
        redirectUri: "https://app.test/callback",
      }),
    ).rejects.toMatchObject({ code: "InvalidCredentialsError" });
    expect(client.session.getSnapshot()).toEqual({ status: "unauthenticated" });
  });

  it("link(), linked() y unlink() delegan en las rutas correspondientes", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(
      client.oauth.link("google", "https://app.test/link-callback"),
    ).resolves.toEqual({ url: "https://google.test/link", state: "st-2" });

    await expect(client.oauth.linked()).resolves.toEqual({ providers: ["google"] });
    await expect(client.oauth.unlink("google")).resolves.toEqual({ unlinked: true });
    await expect(client.oauth.linked()).resolves.toEqual({ providers: [] });
  });
});
