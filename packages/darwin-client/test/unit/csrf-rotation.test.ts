import { describe, expect, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { CookieTransport } from "../../src/transport/cookie";
import { memoryCookieJar } from "../../src/transport/cookie-jar";
import { createFakeFetch, darwinErrorResponse, jsonResponse } from "./helpers/fake-fetch";

/**
 * HC-01: el refresh **rota la sesión** y el token CSRF es `HMAC(sid)`, así que el token cambia en
 * cada refresh. Estos tests fijan, con un servidor falso que aplica esa misma regla, la
 * secuencia completa del cliente: qué token manda cada intento y cuántas veces recupera.
 *
 * El servidor falso es la parte del contrato que importa: un token vale sólo para el `sid`
 * vigente, el refresh lo cambia y lo entrega en la cabecera `X-CSRF-Token`, y `GET /auth/csrf`
 * devuelve el vigente.
 */

const CODIGO_SESION_ANTERIOR = "CsrfTokenFromPreviousSession";

function esperar(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function servidorFalso(
  opciones: { refreshTardaMs?: number; rotarTrasRecuperar?: boolean } = {},
) {
  let sid = 1;
  let accessVencido = false;
  const tokenDe = (n: number) => `csrf-s${n}`;

  /** Lo que mandó cada escritura a `/cosas`, en orden. */
  const escrituras: Array<string | null> = [];
  let refreshes = 0;
  let recuperaciones = 0;

  const { fetchImpl, calls } = createFakeFetch(async (url, init) => {
    const ruta = new URL(url).pathname;
    const metodo = (init?.method ?? "GET").toUpperCase();
    const enviado = new Headers(init?.headers).get("X-CSRF-Token");

    if (ruta === "/auth/sign-in") {
      return jsonResponse({
        body: { session_id: "s1" },
        headers: { "X-CSRF-Token": tokenDe(sid) },
      });
    }

    if (ruta === "/auth/refresh" && metodo === "POST") {
      refreshes += 1;
      if (opciones.refreshTardaMs) await esperar(opciones.refreshTardaMs);
      sid += 1;
      accessVencido = false;
      return jsonResponse({
        body: { session_id: `s${sid}`, expires_in: 120, token_type: "Bearer" },
        headers: { "X-CSRF-Token": tokenDe(sid) },
      });
    }

    if (ruta === "/auth/csrf" && metodo === "GET") {
      recuperaciones += 1;
      const entregado = tokenDe(sid);
      // La sesión vuelve a rotar entre la recuperación y el reintento: el token entregado ya no
      // sirve cuando llega la escritura.
      if (opciones.rotarTrasRecuperar) sid += 1;
      return jsonResponse({ body: { csrf_token: entregado } });
    }

    if (ruta === "/cosas" && metodo === "POST") {
      escrituras.push(enviado);
      if (accessVencido) return darwinErrorResponse(401, "TokenExpiredError");
      if (enviado !== tokenDe(sid)) {
        // El mismo criterio del servidor real: un token de una sesión anterior de la familia
        // lleva su propio `code`; cualquier otro valor, el genérico.
        const eraDeUnaSesionAnterior =
          enviado !== null && /^csrf-s\d+$/.test(enviado) && enviado !== tokenDe(sid);
        return darwinErrorResponse(403, "CsrfValidationError", {
          code: eraDeUnaSesionAnterior ? CODIGO_SESION_ANTERIOR : "CsrfTokenInvalid",
        });
      }
      return jsonResponse({ body: { hecho: true } });
    }

    return darwinErrorResponse(404, "NotFoundError");
  });

  return {
    fetchImpl,
    calls,
    escrituras,
    vencerElAccess: () => {
      accessVencido = true;
    },
    /** Otra pestaña refrescó: el `sid` del servidor cambió sin que este cliente se enterara. */
    rotarPorFuera: () => {
      sid += 1;
    },
    get refreshes() {
      return refreshes;
    },
    get recuperaciones() {
      return recuperaciones;
    },
  };
}

async function clienteConSesion(
  servidor: ReturnType<typeof servidorFalso>,
  extra: { csrfRecovery?: boolean } = {},
) {
  const client = createDarwinClient({
    baseUrl: "https://api.test",
    // Cookie de CSRF ilegible (jar vacío): la API está en otro origen, el token sólo llega por
    // la cabecera de las respuestas y por `GET /auth/csrf`.
    transport: new CookieTransport({ cookieJar: memoryCookieJar() }),
    fetch: servidor.fetchImpl,
    hydrateOnCreate: false,
    ...extra,
  });
  await client.$fetch("/auth/sign-in", { method: "POST", body: { email: "a@b.c" } });
  return client;
}

describe("CSRF y rotación de sesión en el cliente", () => {
  it("401 → refresh → el reintento se vuelve a firmar con el token nuevo", async () => {
    const servidor = servidorFalso();
    const client = await clienteConSesion(servidor);
    servidor.vencerElAccess();

    const resultado = await client.$fetch<{ hecho: boolean }>("/cosas", {
      method: "POST",
      body: { a: 1 },
    });

    expect(resultado).toEqual({ hecho: true });
    // Primer intento con el token de antes del refresh; el reintento, con el de la sesión nueva.
    expect(servidor.escrituras).toEqual(["csrf-s1", "csrf-s2"]);
    expect(servidor.refreshes).toBe(1);
    // Se volvió a firmar sin necesitar la recuperación: el 403 ni siquiera ocurrió.
    expect(servidor.recuperaciones).toBe(0);
  });

  it("un 403 CsrfValidationError dispara UNA recuperación del token y UN reintento", async () => {
    const servidor = servidorFalso();
    const client = await clienteConSesion(servidor);
    servidor.rotarPorFuera();

    const resultado = await client.$fetch<{ hecho: boolean }>("/cosas", {
      method: "POST",
      body: { a: 1 },
    });

    expect(resultado).toEqual({ hecho: true });
    expect(servidor.escrituras).toEqual(["csrf-s1", "csrf-s2"]);
    expect(servidor.recuperaciones).toBe(1);
    expect(servidor.refreshes).toBe(0);
  });

  it("si el reintento también es rechazado, lanza el 403 y no insiste una tercera vez", async () => {
    const servidor = servidorFalso({ rotarTrasRecuperar: true });
    const client = await clienteConSesion(servidor);
    servidor.rotarPorFuera();

    await expect(
      client.$fetch("/cosas", { method: "POST", body: { a: 1 } }),
    ).rejects.toMatchObject({ code: "CsrfValidationError", status: 403 });

    expect(servidor.recuperaciones).toBe(1);
    expect(servidor.escrituras).toHaveLength(2);
  });

  it("el 403 expone el `code` del servidor en `payload`, sin que el cliente lo interprete", async () => {
    const servidor = servidorFalso();
    const client = await clienteConSesion(servidor, { csrfRecovery: false });
    servidor.rotarPorFuera();

    await expect(
      client.$fetch("/cosas", { method: "POST", body: { a: 1 } }),
    ).rejects.toMatchObject({
      code: "CsrfValidationError",
      payload: { code: CODIGO_SESION_ANTERIOR },
    });
  });

  it("con csrfRecovery: false un 403 de CSRF se devuelve tal cual, sin GET /auth/csrf ni reintento", async () => {
    const servidor = servidorFalso();
    const client = await clienteConSesion(servidor, { csrfRecovery: false });
    servidor.rotarPorFuera();

    await expect(
      client.$fetch("/cosas", { method: "POST", body: { a: 1 } }),
    ).rejects.toMatchObject({ code: "CsrfValidationError", status: 403 });

    expect(servidor.recuperaciones).toBe(0);
    expect(servidor.escrituras).toHaveLength(1);
  });

  it("con csrfRecovery: false el reenvío tras un refresh igual se firma con el token vigente", async () => {
    const servidor = servidorFalso();
    const client = await clienteConSesion(servidor, { csrfRecovery: false });
    servidor.vencerElAccess();

    await client.$fetch("/cosas", { method: "POST", body: { a: 1 } });

    expect(servidor.escrituras).toEqual(["csrf-s1", "csrf-s2"]);
  });

  it("una ráfaga concurrente con el access vencido dispara un solo refresh y todas se reenvían firmadas con el token nuevo", async () => {
    // El refresh tarda: mientras está en vuelo llegan los 401 de todas las demás.
    const servidor = servidorFalso({ refreshTardaMs: 20 });
    const client = await clienteConSesion(servidor);
    servidor.vencerElAccess();

    const resultados = await Promise.all(
      Array.from({ length: 6 }, (_, i) =>
        client.$fetch<{ hecho: boolean }>("/cosas", { method: "POST", body: { i } }),
      ),
    );

    expect(resultados.every((r) => r.hecho)).toBe(true);
    expect(servidor.refreshes).toBe(1);
    // Seis primeros intentos con el token viejo y seis reintentos con el nuevo.
    expect(servidor.escrituras.filter((t) => t === "csrf-s1")).toHaveLength(6);
    expect(servidor.escrituras.filter((t) => t === "csrf-s2")).toHaveLength(6);
    expect(servidor.recuperaciones).toBe(0);
  });
});
