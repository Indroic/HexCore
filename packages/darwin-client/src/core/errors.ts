import type { DarwinErrorCode } from "../generated/error-codes";

/**
 * El discriminante de `DarwinError`. Además de los códigos que el backend emite
 * (`DarwinErrorCode`, generado desde `darwin.errors.json`), hay dos que sólo existen del lado
 * del cliente: `NonJsonResponse` (un proxy, un balanceador o un backend roto contestó algo que
 * no es el envelope `{detail, error}`) y `NetworkError` (`fetch` mismo rechazó — sin conexión,
 * CORS, etc.).
 */
//
// `| (string & {})` al final es el truco de "unión abierta": preserva el autocompletado de
// los literales conocidos en el editor y en un `switch`, pero no rechaza en tiempo de
// compilación un código que el backend agregue y este cliente todavía no conozca — ese caso
// es exactamente lo que puede pasar en producción si el servidor se actualiza antes que el
// cliente, y no tiene por qué ser un `NonJsonResponse` (el envelope era válido).
export type DarwinCode =
  | DarwinErrorCode
  | "NonJsonResponse"
  | "NetworkError"
  | (string & {});

/** El header `WWW-Authenticate`, parseado. Se expone pero **no decide** — el discriminante es `code`. */
export interface ParsedWwwAuthenticate {
  scheme: string;
  params: Record<string, string>;
}

export interface DarwinErrorParams {
  code: DarwinCode;
  detail: string;
  status: number | null;
  payload?: Record<string, unknown> | undefined;
  wwwAuthenticate?: ParsedWwwAuthenticate | undefined;
  cause?: unknown;
}

/**
 * El error único de Darwin. Una clase base + `code` literal, no dieciocho subclases: con
 * dieciocho, cada `class TokenExpiredError extends DarwinError {}` sería código muerto que
 * hay que sincronizar a mano con `IDENTITY_EXCEPTION_STATUS_MAP` y los seis plugins. Con
 * `code` generado desde `darwin.errors.json`, un `switch (err.code)` es exhaustivo y el
 * compilador avisa si el backend agrega un código nuevo.
 */
export class DarwinError extends Error {
  readonly code: DarwinCode;
  readonly status: number | null;
  readonly detail: string;
  /** El envelope crudo de la respuesta (`{detail, error, ...extra}`), o `{}` si no hubo cuerpo. */
  readonly payload: Record<string, unknown>;
  readonly wwwAuthenticate: ParsedWwwAuthenticate | undefined;

  constructor(params: DarwinErrorParams) {
    super(params.detail, params.cause !== undefined ? { cause: params.cause } : undefined);
    this.name = "DarwinError";
    this.code = params.code;
    this.status = params.status;
    this.detail = params.detail;
    this.payload = params.payload ?? {};
    this.wwwAuthenticate = params.wwwAuthenticate;
  }
}

/**
 * Los códigos que ameritan un refresh y reintento. Deliberadamente **no** incluye
 * `UnauthenticatedError` (no había ninguna credencial — refrescar no soluciona nada) ni
 * `TokenRevokedError` (la sesión está muerta por detección de reuso: reintentar sería
 * exactamente lo que no hay que hacer, ver `isSessionDead`).
 */
const REFRESHABLE_CODES: ReadonlySet<string> = new Set<DarwinErrorCode>([
  "TokenExpiredError",
  "TokenMalformedError",
  "TokenAudienceMismatchError",
]);

/** Si vale la pena refrescar y reintentar este error. */
export function isRefreshable(err: unknown): err is DarwinError {
  return (
    err instanceof DarwinError && err.status === 401 && REFRESHABLE_CODES.has(err.code)
  );
}

/**
 * Si la sesión está muerta por detección de reuso. Cierre local inmediato **sin** intentar
 * refrescar — ver el docstring de `TokenRevokedError` del lado Python.
 */
export function isSessionDead(err: unknown): err is DarwinError {
  return err instanceof DarwinError && err.code === "TokenRevokedError";
}

/**
 * Códigos que este cliente inventa cuando **no hubo veredicto del servidor** sobre el refresh
 * token: `fetch` mismo falló, o contestó algo que no es el envelope de Darwin. Un balanceador
 * que devolvió basura una vez, o un blip de red al volver de que la laptop estuvo suspendida,
 * no son una respuesta del servidor diciendo que el token es inválido.
 */
const CODIGOS_SIN_VEREDICTO: ReadonlySet<string> = new Set([
  "NetworkError",
  "NonJsonResponse",
]);

/**
 * Si el error es un veredicto real del servidor sobre la validez del refresh token, y no un
 * fallo de transporte. Usado para decidir si un refresh fallido deja la sesión
 * permanentemente muerta (`RefreshController`) o si vale la pena reintentar: tratar un
 * `NetworkError` como "sesión muerta" apagaría sesiones válidas por una falla transitoria que
 * no tiene nada que ver con el refresh token.
 */
export function isDefinitiveAuthFailure(err: unknown): err is DarwinError {
  return err instanceof DarwinError && !CODIGOS_SIN_VEREDICTO.has(err.code);
}

/**
 * Si el motor de autorización (`AuthorizationEngine`, Fase F0 de rbac/drbac) denegó la
 * acción. Angosta `payload` a `{ required: string }` — la acción pedida, sin la razón de la
 * política (ver el docstring de `AccessDeniedError` del lado Python).
 *
 * Un consumidor que atrapa esto es el disparador de `client.rbac.notifyAccessDenied()`: el
 * store no puede interceptar el `$fetch` del cliente entero por su cuenta, porque la mayoría
 * de las peticiones no tienen nada que ver con `rbac`.
 */
export function isAccessDenied(
  err: unknown,
): err is DarwinError & { payload: { required: string } } {
  return (
    err instanceof DarwinError &&
    err.code === "AccessDeniedError" &&
    typeof err.payload.required === "string"
  );
}

/** Si hace falta el segundo factor. Angosta `payload` a `{ challenge: string }` cuando es cierto. */
export function isTwoFactorRequired(
  err: unknown,
): err is DarwinError & { payload: { challenge: string } } {
  return (
    err instanceof DarwinError &&
    err.code === "TwoFactorRequiredError" &&
    typeof err.payload.challenge === "string"
  );
}

/** Parsea `WWW-Authenticate: Bearer realm="hexcore", error="invalid_token"`. */
export function parseWwwAuthenticate(
  header: string | null,
): ParsedWwwAuthenticate | undefined {
  if (!header) return undefined;

  const espacio = header.indexOf(" ");
  const scheme = espacio === -1 ? header : header.slice(0, espacio);
  const resto = espacio === -1 ? "" : header.slice(espacio + 1);

  const params: Record<string, string> = {};
  const re = /([\w-]+)="([^"]*)"/g;
  for (const match of resto.matchAll(re)) {
    const clave = match[1];
    const valor = match[2];
    if (clave !== undefined && valor !== undefined) {
      params[clave] = valor;
    }
  }

  return { scheme, params };
}

/**
 * Arma un `DarwinError` a partir de una `Response` no-OK.
 *
 * Envelope roto (HTML de un proxy, 502 de un balanceador) da `code: "NonJsonResponse"` con
 * los primeros 200 caracteres del body, nunca un `SyntaxError` crudo de `JSON.parse`.
 */
export async function darwinErrorFromResponse(response: Response): Promise<DarwinError> {
  const wwwAuthenticate = parseWwwAuthenticate(response.headers.get("WWW-Authenticate"));

  let texto: string;
  try {
    texto = await response.text();
  } catch (cause) {
    return new DarwinError({
      code: "NetworkError",
      status: response.status,
      detail: "The response body could not be read.",
      wwwAuthenticate,
      cause,
    });
  }

  let json: unknown;
  try {
    json = texto.length > 0 ? JSON.parse(texto) : {};
  } catch {
    return new DarwinError({
      code: "NonJsonResponse",
      status: response.status,
      detail: `The response is not JSON. First 200 characters: ${texto.slice(0, 200)}`,
      wwwAuthenticate,
    });
  }

  if (
    typeof json !== "object" ||
    json === null ||
    typeof (json as Record<string, unknown>).error !== "string"
  ) {
    return new DarwinError({
      code: "NonJsonResponse",
      status: response.status,
      detail:
        `The response does not carry Darwin's envelope ({detail, error}). ` +
        `First 200 characters: ${texto.slice(0, 200)}`,
      wwwAuthenticate,
    });
  }

  const envelope = json as Record<string, unknown>;
  const nombreDeError = envelope.error as string;
  const detail = typeof envelope.detail === "string" ? envelope.detail : nombreDeError;

  return new DarwinError({
    // Un nombre de error que este cliente no tiene en su lista generada igual se preserva
    // tal cual — ver el comentario de `DarwinCode` sobre la unión abierta.
    code: nombreDeError,
    status: response.status,
    detail,
    payload: envelope,
    wwwAuthenticate,
  });
}
