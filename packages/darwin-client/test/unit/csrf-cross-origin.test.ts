import { describe, expect, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { CookieTransport } from "../../src/transport/cookie";
import { memoryCookieJar } from "../../src/transport/cookie-jar";
import {
  countCallsTo,
  createFakeFetch,
  darwinErrorResponse,
  jsonResponse,
} from "./helpers/fake-fetch";

/**
 * HC-03: con la API en otro host que la página, la cookie CSRF no se puede leer. El modo
 * `crossOrigin` del transporte guarda el token **en memoria** y lo pide con `GET /auth/csrf`
 * **antes de la primera escritura** si la página se recargó y no lo tiene.
 *
 * El servidor falso aplica la regla real: una escritura con cookie de sesión sólo pasa si el
 * header `X-CSRF-Token` trae el token de la sesión; sin sesión no se exige nada y
 * `GET /auth/csrf` da 401.
 */

const TOKEN = "csrf-de-la-sesion";

function servidorFalso(opciones: { conSesion: boolean }) {
  let conSesion = opciones.conSesion;
  const escrituras: Array<string | null> = [];

  const { fetchImpl, calls } = createFakeFetch(async (url, init) => {
    const ruta = new URL(url).pathname;
    const metodo = (init?.method ?? "GET").toUpperCase();
    const enviado = new Headers(init?.headers).get("X-CSRF-Token");

    if (ruta === "/auth/csrf" && metodo === "GET") {
      // Da tiempo a que otras escrituras lleguen mientras esta sigue en vuelo.
      await new Promise((resolve) => setTimeout(resolve, 5));
      if (!conSesion) return darwinErrorResponse(401, "UnauthenticatedError");
      return jsonResponse({ body: { csrf_token: TOKEN } });
    }

    if (ruta === "/auth/sign-in" && metodo === "POST") {
      conSesion = true;
      return jsonResponse({
        body: { session_id: "s1" },
        headers: { "X-CSRF-Token": TOKEN },
      });
    }

    if (ruta === "/cosas" && metodo === "POST") {
      escrituras.push(enviado);
      if (conSesion && enviado !== TOKEN) {
        return darwinErrorResponse(403, "CsrfValidationError", {
          code: "CsrfTokenInvalid",
        });
      }
      return jsonResponse({ body: { hecho: true } });
    }

    if (ruta === "/cosas" && metodo === "GET") return jsonResponse({ body: { lista: [] } });

    return darwinErrorResponse(404, "NotFoundError");
  });

  return { fetchImpl, calls, escrituras };
}

function clienteCrossOrigin(
  servidor: ReturnType<typeof servidorFalso>,
  extra: { csrfRecovery?: boolean } = {},
) {
  return createDarwinClient({
    baseUrl: "https://api.test",
    // Sin `cookieJar`: el modo cross-origin no lo necesita y esto corre fuera de un navegador.
    transport: new CookieTransport({ crossOrigin: true }),
    fetch: servidor.fetchImpl,
    hydrateOnCreate: false,
    ...extra,
  });
}

describe("CookieTransport en modo crossOrigin", () => {
  it("se construye sin cookieJar y fuera de un navegador", () => {
    expect(() => new CookieTransport({ crossOrigin: true })).not.toThrow();
  });

  it("no lee la cookie aunque el jar la tenga: cross-origin sólo vale el token en memoria", async () => {
    const transporte = new CookieTransport({
      crossOrigin: true,
      cookieJar: memoryCookieJar("__Host-csrf=de-la-cookie-ajena"),
    });
    transporte.onResponse(
      new Response("", { headers: { "X-CSRF-Token": "de-la-cabecera" } }),
    );

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBe("de-la-cabecera");
  });

  it("pide el token por adelantado sólo en escrituras, y sólo si no lo tiene", () => {
    const transporte = new CookieTransport({ crossOrigin: true });

    expect(transporte.needsCsrfToken("POST")).toBe(true);
    expect(transporte.needsCsrfToken("GET")).toBe(false);
    expect(transporte.needsCsrfToken("OPTIONS")).toBe(false);

    transporte.setCsrfToken("t");
    expect(transporte.needsCsrfToken("POST")).toBe(false);
  });

  it("sin crossOrigin no pide nada por adelantado", () => {
    const transporte = new CookieTransport({ cookieJar: memoryCookieJar() });

    expect(transporte.needsCsrfToken("POST")).toBe(false);
  });

  it("tras preguntar y no haber sesión, no vuelve a pedir hasta que haya una", () => {
    const transporte = new CookieTransport({ crossOrigin: true });

    transporte.setCsrfToken(undefined);
    expect(transporte.needsCsrfToken("POST")).toBe(false);

    transporte.persist({ sessionId: "s1", expiresIn: 120, tokenType: "Bearer" });
    expect(transporte.needsCsrfToken("POST")).toBe(true);
  });

  it("clear (sign-out) olvida el token y no vuelve a pedirlo", () => {
    const transporte = new CookieTransport({ crossOrigin: true });
    transporte.setCsrfToken("t");

    transporte.clear();

    expect(transporte.needsCsrfToken("POST")).toBe(false);
  });
});

describe("cliente en modo crossOrigin", () => {
  it("primera escritura tras recargar la página: pide el token y la escritura sale bien, sin pasar por un 403", async () => {
    const servidor = servidorFalso({ conSesion: true });
    const client = clienteCrossOrigin(servidor);

    const resultado = await client.$fetch<{ hecho: boolean }>("/cosas", {
      method: "POST",
      body: { a: 1 },
    });

    expect(resultado).toEqual({ hecho: true });
    // Un solo intento de escritura, ya firmado: el 403 de recuperación no hizo falta.
    expect(servidor.escrituras).toEqual([TOKEN]);
    expect(countCallsTo(servidor.calls, "/auth/csrf")).toBe(1);
    expect(servidor.calls.map((c) => new URL(c.url).pathname)).toEqual([
      "/auth/csrf",
      "/cosas",
    ]);
  });

  it("la segunda escritura reutiliza el token en memoria", async () => {
    const servidor = servidorFalso({ conSesion: true });
    const client = clienteCrossOrigin(servidor);

    await client.$fetch("/cosas", { method: "POST", body: {} });
    await client.$fetch("/cosas", { method: "POST", body: {} });

    expect(servidor.escrituras).toEqual([TOKEN, TOKEN]);
    expect(countCallsTo(servidor.calls, "/auth/csrf")).toBe(1);
  });

  it("una ráfaga de escrituras en la primera carga dispara un solo GET /auth/csrf", async () => {
    const servidor = servidorFalso({ conSesion: true });
    const client = clienteCrossOrigin(servidor);

    await Promise.all([
      client.$fetch("/cosas", { method: "POST", body: { n: 1 } }),
      client.$fetch("/cosas", { method: "POST", body: { n: 2 } }),
      client.$fetch("/cosas", { method: "POST", body: { n: 3 } }),
    ]);

    expect(countCallsTo(servidor.calls, "/auth/csrf")).toBe(1);
    expect(servidor.escrituras).toEqual([TOKEN, TOKEN, TOKEN]);
  });

  it("una lectura no pide el token", async () => {
    const servidor = servidorFalso({ conSesion: true });
    const client = clienteCrossOrigin(servidor);

    await client.$fetch("/cosas");

    expect(countCallsTo(servidor.calls, "/auth/csrf")).toBe(0);
  });

  it("sin sesión la escritura sale igual, y no se vuelve a preguntar en la siguiente", async () => {
    const servidor = servidorFalso({ conSesion: false });
    const client = clienteCrossOrigin(servidor);

    await client.$fetch("/cosas", { method: "POST", body: {} });
    await client.$fetch("/cosas", { method: "POST", body: {} });

    // Una sola pregunta (el 401 de la primera): la segunda escritura anónima no paga otra.
    expect(countCallsTo(servidor.calls, "/auth/csrf")).toBe(1);
    expect(servidor.escrituras).toEqual([null, null]);
  });

  it("tras el sign-in, la cabecera de su respuesta es el token de las escrituras", async () => {
    const servidor = servidorFalso({ conSesion: false });
    const client = clienteCrossOrigin(servidor);

    await client.$fetch("/auth/sign-in", { method: "POST", body: { email: "a@b.c" } });
    await client.$fetch("/cosas", { method: "POST", body: {} });

    expect(servidor.escrituras).toEqual([TOKEN]);
    // Sólo el 401 de antes del sign-in: después el token ya estaba en memoria.
    expect(countCallsTo(servidor.calls, "/auth/csrf")).toBe(1);
  });

  it("con csrfRecovery: false el token se pide igual por adelantado", async () => {
    const servidor = servidorFalso({ conSesion: true });
    const client = clienteCrossOrigin(servidor, { csrfRecovery: false });

    await client.$fetch("/cosas", { method: "POST", body: {} });

    expect(servidor.escrituras).toEqual([TOKEN]);
  });
});
