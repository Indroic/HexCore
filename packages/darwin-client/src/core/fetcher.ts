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

export function createFetcher(options: FetcherOptions): Fetcher {
  async function $fetch<T>(path: string, init: RequestOptions = {}): Promise<T> {
    return doFetch<T>(path, init, 0);
  }

  async function doFetch<T>(
    path: string,
    init: RequestOptions,
    retryCount: number,
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

    if (response.ok) {
      return leerCuerpoExitoso<T>(response);
    }

    const error = await darwinErrorFromResponse(response);

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
      return doFetch<T>(path, init, retryCount + 1);
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
