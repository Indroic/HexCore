/** Un `fetch` programable para tests: responde según una cola o un handler por ruta. */

export interface FakeResponseSpec {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}

export function jsonResponse(spec: FakeResponseSpec = {}): Response {
  const status = spec.status ?? 200;
  const body = spec.body === undefined ? "" : JSON.stringify(spec.body);
  return new Response(body, { status, ...(spec.headers ? { headers: spec.headers } : {}) });
}

export function darwinErrorResponse(
  status: number,
  errorName: string,
  extra: Record<string, unknown> = {},
): Response {
  return jsonResponse({
    status,
    body: { detail: `detalle de ${errorName}`, error: errorName, ...extra },
  });
}

/**
 * Crea un fake `fetch` que devuelve, en orden, una respuesta por cada llamada a `handler` para
 * la ruta pedida. `calls` registra cada invocación para asertar cuántas veces se llamó a qué.
 */
export function createFakeFetch(
  handler: (
    url: string,
    init: RequestInit | undefined,
    callIndex: number,
  ) => Response | Promise<Response>,
) {
  const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
  let callIndex = 0;

  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const urlStr = String(url);
    calls.push({ url: urlStr, init });
    const respuesta = await handler(urlStr, init, callIndex);
    callIndex += 1;
    return respuesta;
  }) as typeof fetch;

  return { fetchImpl, calls };
}

/** Cuenta las llamadas cuya URL termina en `path`. */
export function countCallsTo(calls: Array<{ url: string }>, path: string): number {
  return calls.filter((c) => c.url.endsWith(path)).length;
}
