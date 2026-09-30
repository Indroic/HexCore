import type { Transport } from "../transport";
import { DarwinError, darwinErrorFromResponse, isRefreshable } from "./errors";
import { joinUrl } from "./url";

export interface FetcherOptions {
  baseUrl: string;
  transport: Transport;
  fetchImpl: typeof fetch;
  /** Si conviene refrescar antes de este request (el reloj local corrido, o el TTL vencido). */
  shouldRefreshProactively: () => boolean;
  /**
   * El refresh en sí. Es responsabilidad de quien arma el fetcher que sea single-flight — ver
   * `session/refresh.ts`: sin eso, N requests en paralelo con el token vencido dispararían N
   * refreshes, y el servidor rota el token en el primero y revoca la familia entera con los
   * N-1 restantes.
   */
  refresh: () => Promise<void>;
}

export interface RequestOptions extends Omit<RequestInit, "body"> {
  /** Un objeto plano se serializa a JSON; un `BodyInit` (string, FormData, Blob...) viaja tal cual. */
  body?: unknown;
  /** Para el propio refresh: no evalúes el proactivo antes de refrescar, sería recursivo. */
  skipProactiveRefresh?: boolean;
  /** Para el propio refresh: no reintentes un 401 del refresh contra el refresh. */
  skipReactiveRetry?: boolean;
}

export interface Fetcher {
  $fetch<T>(path: string, init?: RequestOptions): Promise<T>;
}

/** Los métodos que el CSRF no protege: un rechazo de CSRF no puede venir de ellos. */
const METODOS_SEGUROS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Ruta relativa a `baseUrl` de `GET /auth/csrf` (ver `build_identity_router`). */
const RUTA_CSRF = "/auth/csrf";

export function createFetcher(options: FetcherOptions): Fetcher {
  async function $fetch<T>(path: string, init: RequestOptions = {}): Promise<T> {
    return doFetch<T>(path, init, 0, false);
  }

  /**
   * Pide el token CSRF de la sesión por cookie en curso. **Nunca lanza**: sin sesión (401), sin
   * red o con una respuesta inesperada devuelve `undefined`, y quien lo pidió responde con el
   * rechazo de CSRF original, que sí dice qué pasó.
   */
  async function pedirTokenCsrf(): Promise<string | undefined> {
    try {
      const respuesta = await options.fetchImpl(joinUrl(options.baseUrl, RUTA_CSRF), {
        method: "GET",
        // La cookie de sesión es lo que identifica de qué sesión es el token.
        credentials: "include",
        headers: { Accept: "application/json", "X-Darwin-Transport": "cookie" },
      });
      if (!respuesta.ok) return undefined;
      const cuerpo = (await respuesta.json()) as { csrf_token?: unknown };
      return typeof cuerpo.csrf_token === "string" && cuerpo.csrf_token
        ? cuerpo.csrf_token
        : undefined;
    } catch {
      return undefined;
    }
  }

  async function doFetch<T>(
    path: string,
    init: RequestOptions,
    retryCount: number,
    csrfReintentado: boolean,
  ): Promise<T> {
    if (!init.skipProactiveRefresh && options.shouldRefreshProactively()) {
      await options.refresh();
    }

    const method = (init.method ?? "GET").toUpperCase();
    const transportHeaders = await options.transport.requestHeaders(method);

    const headers = new Headers(init.headers);
    for (const [clave, valor] of Object.entries(transportHeaders)) {
      headers.set(clave, valor);
    }

    const body = prepararBody(init.body, headers);
    // `exactOptionalPropertyTypes` no deja que `body`/`credentials` lleguen como `undefined`
    // explícito a `RequestInit` (que los declara `T | null`, no `T | undefined`); se arma el
    // resto de las opciones sin nuestros campos propios y se agregan `body`/`credentials`
    // sólo cuando de verdad hay un valor.
    const {
      body: _bodyIgnorado,
      skipProactiveRefresh: _sprIgnorado,
      skipReactiveRetry: _srrIgnorado,
      headers: _headersIgnorados,
      ...pasoDirecto
    } = init;

    let response: Response;
    try {
      response = await options.fetchImpl(joinUrl(options.baseUrl, path), {
        ...pasoDirecto,
        method,
        headers,
        ...(body !== undefined ? { body } : {}),
        ...(options.transport.name === "cookie"
          ? { credentials: "include" as const }
          : init.credentials !== undefined
            ? { credentials: init.credentials }
            : {}),
      });
    } catch (cause) {
      throw new DarwinError({
        code: "NetworkError",
        status: null,
        detail:
          "The request never completed (no connection, CORS, or the server did not answer).",
        cause,
      });
    }

    // Antes de leer el cuerpo y también en los errores: un 401 o un 403 no traen la cabecera,
    // pero un refresh que sí la trae puede llegar por cualquier camino.
    options.transport.onResponse?.(response);

    if (response.ok) {
      return leerCuerpoExitoso<T>(response);
    }

    const error = await darwinErrorFromResponse(response);

    // Un 403 `CsrfValidationError` en una escritura por cookie: el token que se mandó no vale
    // (la página se recargó y ya no lo tiene en memoria, o la sesión rotó). Se pide el vigente y
    // se reintenta **una sola vez**. Es seguro reintentar: el middleware de CSRF rechaza antes de
    // que corra el handler, así que el efecto de lado del request original no ocurrió. Si el
    // token nuevo es el mismo que ya se mandó, reintentar no lo arreglaría: el rechazo es de otra
    // causa (un origen que el servidor no confía) y se devuelve tal cual.
    if (
      options.transport.name === "cookie" &&
      !csrfReintentado &&
      response.status === 403 &&
      error.code === "CsrfValidationError" &&
      !METODOS_SEGUROS.has(method) &&
      esReenviable(init.body)
    ) {
      const fresco = await pedirTokenCsrf();
      if (fresco !== undefined && fresco !== headers.get("X-CSRF-Token")) {
        options.transport.setCsrfToken?.(fresco);
        return doFetch<T>(path, init, retryCount, true);
      }
    }

    // Reintentar es seguro exactamente cuando: (1) es un 401 con un código refrescable, (2) es
    // el primer intento (nunca un segundo reintento — un refresh que sigue fallando no se
    // arregla insistiendo). Un 401 con envelope de identidad lo emite el middleware **antes**
    // de que corra el handler, así que el efecto de lado del request original no ocurrió —
    // reintentar un POST acá no duplica nada.
    if (
      !init.skipReactiveRetry &&
      retryCount === 0 &&
      response.status === 401 &&
      isRefreshable(error)
    ) {
      if (!esReenviable(init.body)) {
        throw new DarwinError({
          code: error.code,
          status: error.status,
          payload: error.payload,
          wwwAuthenticate: error.wwwAuthenticate,
          detail:
            `${error.detail} (the request body was an already-consumed stream and cannot ` +
            "be replayed after the refresh; pass it as a plain object, string, FormData or " +
            "Blob if you need this case to retry on its own)",
        });
      }

      await options.refresh();
      return doFetch<T>(path, init, retryCount + 1, csrfReintentado);
    }

    throw error;
  }

  return { $fetch };
}

async function leerCuerpoExitoso<T>(response: Response): Promise<T> {
  const texto = await response.text();
  if (texto.length === 0) return undefined as T;

  try {
    return JSON.parse(texto) as T;
  } catch {
    throw new DarwinError({
      code: "NonJsonResponse",
      status: response.status,
      detail: `The response is not JSON. First 200 characters: ${texto.slice(0, 200)}`,
    });
  }
}

function prepararBody(body: unknown, headers: Headers): BodyInit | undefined {
  if (body === undefined || body === null) return undefined;

  if (
    typeof body === "string" ||
    body instanceof FormData ||
    body instanceof Blob ||
    body instanceof URLSearchParams ||
    body instanceof ReadableStream ||
    body instanceof ArrayBuffer
  ) {
    return body as BodyInit;
  }

  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return JSON.stringify(body);
}

/**
 * Si el cuerpo del request se puede volver a mandar en un reintento. Un `ReadableStream` ya
 * empezado a leer no se puede rebobinar; todo lo demás (objetos planos que se re-serializan,
 * strings, `FormData`, `Blob`) sí.
 */
function esReenviable(body: unknown): boolean {
  if (body instanceof ReadableStream) return false;
  return true;
}
