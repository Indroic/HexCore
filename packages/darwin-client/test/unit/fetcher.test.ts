import { describe, expect, it, vi } from "vitest";
import { DarwinError } from "../../src/core/errors";
import { createFetcher } from "../../src/core/fetcher";
import type { Transport } from "../../src/transport";
import { CookieTransport } from "../../src/transport/cookie";
import { memoryCookieJar } from "../../src/transport/cookie-jar";
import { createFakeFetch, darwinErrorResponse, jsonResponse } from "./helpers/fake-fetch";

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
    ).rejects.toThrow(/cannot be replayed/);
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

// ── Token CSRF con la API en otro origen ──────────────────────────────────────────────────────
//
// El cliente no puede leer la cookie de CSRF (es del host de la API): el token llega en la
// cabecera `X-CSRF-Token` de lo que emite la cookie, y `GET /auth/csrf` lo recupera cuando la
// página se recarga con una sesión que ya existe.
describe("createFetcher: token CSRF sin cookie legible", () => {
  const csrfRechazado = () => darwinErrorResponse(403, "CsrfValidationError");

  function armar(handler: Parameters<typeof createFakeFetch>[0], transport?: Transport) {
    const { fetchImpl, calls } = createFakeFetch(handler);
    const cookie = transport ?? new CookieTransport({ cookieJar: memoryCookieJar() });
    const fetcher = createFetcher({
      baseUrl: "https://api.test",
      transport: cookie,
      fetchImpl,
      shouldRefreshProactively: () => false,
      refresh: vi.fn(),
    });
    return { fetcher, calls, transport: cookie };
  }

  function tokenEnviado(init: RequestInit | undefined): string | null {
    return new Headers(init?.headers).get("X-CSRF-Token");
  }

  it("guarda el token de la cabecera de una respuesta y lo manda en la siguiente escritura", async () => {
    const { fetcher, calls } = armar((url) =>
      url.endsWith("/auth/sign-in")
        ? jsonResponse({
            body: { session_id: "s1" },
            headers: { "X-CSRF-Token": "del-sign-in" },
          })
        : jsonResponse({ body: { ok: true } }),
    );

    await fetcher.$fetch("/auth/sign-in", { method: "POST", body: { a: 1 } });
    await fetcher.$fetch("/cosas", { method: "POST", body: { b: 2 } });

    expect(tokenEnviado(calls[0]?.init)).toBeNull();
    expect(tokenEnviado(calls[1]?.init)).toBe("del-sign-in");
  });

  it("ante un 403 CsrfValidationError pide GET /auth/csrf y reintenta UNA vez con el token nuevo", async () => {
    const { fetcher, calls } = armar((url, _init, i) => {
      if (url.endsWith("/auth/csrf"))
        return jsonResponse({ body: { csrf_token: "fresco" } });
      return i === 0 ? csrfRechazado() : jsonResponse({ body: { hecho: true } });
    });

    const resultado = await fetcher.$fetch<{ hecho: boolean }>("/cosas", {
      method: "POST",
      body: { a: 1 },
    });

    expect(resultado).toEqual({ hecho: true });
    expect(calls.map((c) => c.url)).toEqual([
      "https://api.test/cosas",
      "https://api.test/auth/csrf",
      "https://api.test/cosas",
    ]);
    // La petición del token lleva la cookie de sesión (es lo que la identifica) y es un GET.
    expect(calls[1]?.init?.credentials).toBe("include");
    expect(calls[1]?.init?.method).toBe("GET");
    expect(tokenEnviado(calls[2]?.init)).toBe("fresco");
  });

  it("el reintento reenvía el mismo cuerpo", async () => {
    const { fetcher, calls } = armar((url, _init, i) => {
      if (url.endsWith("/auth/csrf"))
        return jsonResponse({ body: { csrf_token: "fresco" } });
      return i === 0 ? csrfRechazado() : jsonResponse({ body: {} });
    });

    await fetcher.$fetch("/cosas", { method: "POST", body: { monto: 5 } });

    expect(calls[0]?.init?.body).toBe('{"monto":5}');
    expect(calls[2]?.init?.body).toBe('{"monto":5}');
  });

  it("si el segundo intento también es rechazado, lo lanza: no insiste", async () => {
    const { fetcher, calls } = armar((url) =>
      url.endsWith("/auth/csrf")
        ? jsonResponse({ body: { csrf_token: "fresco" } })
        : csrfRechazado(),
    );

    await expect(
      fetcher.$fetch("/cosas", { method: "POST", body: {} }),
    ).rejects.toMatchObject({
      code: "CsrfValidationError",
    });
    expect(calls.filter((c) => c.url.endsWith("/cosas"))).toHaveLength(2);
    expect(calls.filter((c) => c.url.endsWith("/auth/csrf"))).toHaveLength(1);
  });

  it("no reintenta si el token nuevo es el mismo que ya se mandó: no lo arreglaría", async () => {
    const jar = memoryCookieJar("__Host-csrf=igual");
    const { fetcher, calls } = armar(
      (url) =>
        url.endsWith("/auth/csrf")
          ? jsonResponse({ body: { csrf_token: "igual" } })
          : csrfRechazado(),
      new CookieTransport({ cookieJar: jar }),
    );

    await expect(
      fetcher.$fetch("/cosas", { method: "POST", body: {} }),
    ).rejects.toMatchObject({
      code: "CsrfValidationError",
    });
    expect(calls.filter((c) => c.url.endsWith("/cosas"))).toHaveLength(1);
  });

  it("sin sesión (GET /auth/csrf da 401) lanza el rechazo original", async () => {
    const { fetcher } = armar((url) =>
      url.endsWith("/auth/csrf")
        ? darwinErrorResponse(401, "UnauthenticatedError")
        : csrfRechazado(),
    );

    await expect(
      fetcher.$fetch("/cosas", { method: "POST", body: {} }),
    ).rejects.toMatchObject({
      code: "CsrfValidationError",
    });
  });

  it("si GET /auth/csrf falla por red, lanza el rechazo original y no un NetworkError", async () => {
    const { fetcher } = armar((url) => {
      if (url.endsWith("/auth/csrf")) throw new TypeError("failed to fetch");
      return csrfRechazado();
    });

    await expect(
      fetcher.$fetch("/cosas", { method: "POST", body: {} }),
    ).rejects.toMatchObject({
      code: "CsrfValidationError",
    });
  });

  it("no reintenta un 403 que no es de CSRF", async () => {
    const { fetcher, calls } = armar(() => darwinErrorResponse(403, "AccessDeniedError"));

    await expect(
      fetcher.$fetch("/cosas", { method: "POST", body: {} }),
    ).rejects.toMatchObject({
      code: "AccessDeniedError",
    });
    expect(calls).toHaveLength(1);
  });

  it("no reintenta un método seguro", async () => {
    const { fetcher, calls } = armar(() => csrfRechazado());

    await expect(fetcher.$fetch("/cosas")).rejects.toMatchObject({
      code: "CsrfValidationError",
    });
    expect(calls).toHaveLength(1);
  });

  it("no reintenta con el transporte Bearer: no hace CSRF", async () => {
    const { fetcher, calls } = armar(() => csrfRechazado(), fakeTransport("bearer"));

    await expect(
      fetcher.$fetch("/cosas", { method: "POST", body: {} }),
    ).rejects.toMatchObject({
      code: "CsrfValidationError",
    });
    expect(calls).toHaveLength(1);
  });

  it("no reintenta un cuerpo que ya se consumió (un stream)", async () => {
    const { fetcher, calls } = armar((url) =>
      url.endsWith("/auth/csrf")
        ? jsonResponse({ body: { csrf_token: "fresco" } })
        : csrfRechazado(),
    );

    await expect(
      fetcher.$fetch("/cosas", { method: "POST", body: new ReadableStream() }),
    ).rejects.toMatchObject({ code: "CsrfValidationError" });
    expect(calls.filter((c) => c.url.endsWith("/cosas"))).toHaveLength(1);
  });
});
