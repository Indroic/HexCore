import { describe, expect, it, vi } from "vitest";
import { DarwinError } from "../../src/core/errors";
import { createFetcher } from "../../src/core/fetcher";
import type { Transport } from "../../src/transport";
import { darwinErrorResponse, jsonResponse } from "./helpers/fake-fetch";

function fakeTransport(name: "cookie" | "bearer" = "bearer"): Transport {
  return {
    name,
    requestHeaders: vi.fn(async () => ({ "X-Darwin-Transport": name })),
    persist: vi.fn(),
    clear: vi.fn(),
  };
}

describe("createFetcher", () => {
  it("manda X-Darwin-Transport en absolutamente todos los requests", async () => {
    const transport = fakeTransport("bearer");
    const fetchImpl = vi.fn(async () => jsonResponse({ body: { ok: true } }));

    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport,
      fetchImpl,
      shouldRefreshProactively: () => false,
      refresh: vi.fn(),
    });

    await fetcher.$fetch("/algo");
    await fetcher.$fetch("/otra-cosa", { method: "POST", body: { a: 1 } });

    expect(transport.requestHeaders).toHaveBeenCalledTimes(2);
    const [primeraCall] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(primeraCall).toContain("/algo");
  });

  it("devuelve el JSON parseado en una respuesta exitosa", async () => {
    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl: vi.fn(async () => jsonResponse({ body: { hola: "mundo" } })),
      shouldRefreshProactively: () => false,
      refresh: vi.fn(),
    });

    const resultado = await fetcher.$fetch<{ hola: string }>("/algo");
    expect(resultado).toEqual({ hola: "mundo" });
  });

  it("envuelve un fetch que rechaza en un DarwinError NetworkError", async () => {
    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl: vi.fn(async () => {
        throw new TypeError("failed to fetch");
      }),
      shouldRefreshProactively: () => false,
      refresh: vi.fn(),
    });

    await expect(fetcher.$fetch("/algo")).rejects.toMatchObject({
      code: "NetworkError",
    });
  });

  it("evalúa el refresh proactivo antes de mandar el request", async () => {
    const refresh = vi.fn(async () => {});
    const fetchImpl = vi.fn(async () => jsonResponse({ body: {} }));

    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl,
      shouldRefreshProactively: () => true,
      refresh,
    });

    await fetcher.$fetch("/algo");

    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("no evalúa el refresh proactivo cuando skipProactiveRefresh", async () => {
    const refresh = vi.fn(async () => {});
    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl: vi.fn(async () => jsonResponse({ body: {} })),
      shouldRefreshProactively: () => true,
      refresh,
    });

    await fetcher.$fetch("/auth/refresh", { skipProactiveRefresh: true });

    expect(refresh).not.toHaveBeenCalled();
  });

  it("un 401 refrescable: refresca una vez y reintenta", async () => {
    let intento = 0;
    const fetchImpl = vi.fn(async () => {
      intento += 1;
      if (intento === 1) return darwinErrorResponse(401, "TokenExpiredError");
      return jsonResponse({ body: { ok: true } });
    });
    const refresh = vi.fn(async () => {});

    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl,
      shouldRefreshProactively: () => false,
      refresh,
    });

    const resultado = await fetcher.$fetch("/protegido");

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(resultado).toEqual({ ok: true });
  });

  it("un 401 no refrescable no dispara refresh y lanza", async () => {
    const refresh = vi.fn(async () => {});
    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl: vi.fn(async () => darwinErrorResponse(401, "InvalidCredentialsError")),
      shouldRefreshProactively: () => false,
      refresh,
    });

    await expect(fetcher.$fetch("/auth/sign-in")).rejects.toMatchObject({
      code: "InvalidCredentialsError",
    });
    expect(refresh).not.toHaveBeenCalled();
  });

  it("un segundo 401 tras el reintento no vuelve a refrescar (un solo reintento)", async () => {
    const fetchImpl = vi.fn(async () => darwinErrorResponse(401, "TokenExpiredError"));
    const refresh = vi.fn(async () => {});

    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl,
      shouldRefreshProactively: () => false,
      refresh,
    });

    await expect(fetcher.$fetch("/protegido")).rejects.toMatchObject({
      code: "TokenExpiredError",
    });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("un body no reenviable (stream ya consumido) no se reintenta", async () => {
    const fetchImpl = vi.fn(async () => darwinErrorResponse(401, "TokenExpiredError"));
    const refresh = vi.fn(async () => {});

    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl,
      shouldRefreshProactively: () => false,
      refresh,
    });

    const stream = new ReadableStream();
    await expect(
      fetcher.$fetch("/protegido", { method: "POST", body: stream }),
    ).rejects.toThrow(/no se puede reenviar/);
    expect(refresh).not.toHaveBeenCalled();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("credentials: include sólo en transporte cookie", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ body: {} }));

    const fetcherCookie = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport("cookie"),
      fetchImpl,
      shouldRefreshProactively: () => false,
      refresh: vi.fn(),
    });
    await fetcherCookie.$fetch("/algo");

    const [, initCookie] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(initCookie.credentials).toBe("include");

    fetchImpl.mockClear();
    const fetcherBearer = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport("bearer"),
      fetchImpl,
      shouldRefreshProactively: () => false,
      refresh: vi.fn(),
    });
    await fetcherBearer.$fetch("/algo");

    const [, initBearer] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(initBearer.credentials).toBeUndefined();
  });

  it("una respuesta 200 que no es JSON da DarwinError NonJsonResponse", async () => {
    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: fakeTransport(),
      fetchImpl: vi.fn(
        async () => new Response("<html>no soy json</html>", { status: 200 }),
      ),
      shouldRefreshProactively: () => false,
      refresh: vi.fn(),
    });

    await expect(fetcher.$fetch("/algo")).rejects.toBeInstanceOf(DarwinError);
    await expect(fetcher.$fetch("/algo")).rejects.toMatchObject({
      code: "NonJsonResponse",
    });
  });
});
