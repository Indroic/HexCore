import type { components } from "../generated/schema";
import type { Transport } from "../transport";

/** La respuesta de un sign-in, un refresh, o cualquier ruta que abre/rota una sesión. */
export type SessionResponse = components["schemas"]["SessionResponse"];

/** Quién sos, y a nombre de quién estás actuando (`GET /auth/me`). */
export type MeResponse = components["schemas"]["MeResponse"];

/**
 * El cuerpo de `POST /auth/sign-in`.
 *
 * El backend acepta el identificador con tres nombres —`identifier`, `email` y `username`—
 * porque `email` era el único hasta la 9.x. El cliente manda siempre `identifier`: los otros
 * dos existen para que un front viejo siga andando contra un backend nuevo, no para elegir.
 */
export type SignInRequest = components["schemas"]["SignInRequest"];

export interface DarwinClientOptions {
  /** La URL base de Darwin, sin `/auth` — por ejemplo `"https://api.miapp.com"`. */
  baseUrl: string;

  /** El transporte: `new CookieTransport(...)` o `new BearerTransport(...)`. */
  transport: Transport;

  /**
   * Si hidrata la sesión (`GET /auth/me`) al construir el cliente. Por defecto `true`.
   *
   * En SSR pasá `false`: con `hydrateOnCreate: false` el estado inicial del store es
   * `unauthenticated` en vez de `loading` — un `loading` eterno del lado del servidor es un
   * spinner renderizado en el HTML, porque el servidor nunca llega a resolver la hidratación.
   */
  hydrateOnCreate?: boolean;

  /** El `fetch` a usar. Por defecto, el global. Inyectable para tests y para runtimes sin uno propio. */
  fetch?: typeof fetch;
}

/**
 * El resultado de `signIn()`. Discriminado y no una excepción: 2FA activado no es un error de
 * programación ni una falla, es el camino feliz de una cuenta bien configurada. Modelarlo como
 * excepción obligaría a que toda app envuelva su login en `try/catch` y distinga a mano entre
 * "credenciales malas" (que sí es error) y "falta el segundo paso" (que es progreso).
 */
export type SignInResult =
  | { status: "signed-in"; session: SessionResponse }
  | { status: "two-factor-required"; challenge: string };

/**
 * El estado del store de sesión.
 *
 * `reason` en `unauthenticated` distingue por qué se llegó ahí: `"signed-out"` es un cierre
 * deliberado, `"refresh-failed"` es un refresh que falló (sesión muerta, marcada para no
 * reintentar contra un refresh que ya se sabe caído), y `"revoked"` es una detección de reuso
 * — la app puede mostrar "tu sesión se cerró por seguridad" en vez de un logout mudo.
 */
export type SessionState =
  | { status: "loading" }
  | { status: "unauthenticated"; reason?: "signed-out" | "refresh-failed" | "revoked" }
  | { status: "authenticated"; me: MeResponse };
