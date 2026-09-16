import type { SessionResponse, SignInResult } from "../core/types";
import { definePlugin } from "../plugins";

/**
 * Las opciones tal como las entiende `navigator.credentials.create()`/`.get()` — JSON crudo,
 * con la forma de `PublicKeyCredentialCreationOptions`/`RequestOptions` del spec WebAuthn
 * (https://www.w3.org/TR/webauthn-3/). No se remodelan acá a propósito: son un estándar ajeno,
 * y el paso de codificar/decodificar base64url y llamar a `navigator.credentials` vive en el
 * subpath `@hexcore-js/darwin-client/webauthn`, que sí conoce el navegador. Este plugin es el
 * transporte HTTP puro — corre igual en Node que en un browser.
 */
export type WebAuthnJSON = Record<string, unknown>;

/** Una credencial ya en su forma serializada (`PublicKeyCredential.toJSON()` o equivalente). */
export type WebAuthnCredentialJSON = Record<string, unknown>;

export interface PasskeySummary {
  id: string;
  name: string | null;
  aaguid: string | null;
  backedUp: boolean;
  createdAt: string | null;
  lastUsedAt: string | null;
}

interface PasskeySummaryDelServidor {
  id: string;
  name: string | null;
  aaguid: string | null;
  backed_up: boolean;
  created_at: string | null;
  last_used_at: string | null;
}

function aResumen(p: PasskeySummaryDelServidor): PasskeySummary {
  return {
    id: p.id,
    name: p.name,
    aaguid: p.aaguid,
    backedUp: p.backed_up,
    createdAt: p.created_at,
    lastUsedAt: p.last_used_at,
  };
}

export interface PasskeyApi {
  /** Las opciones para `navigator.credentials.create()`. Exige sesión: registra al actor del contexto. */
  registerOptions: () => Promise<WebAuthnJSON>;
  /** Guarda la credencial creada con las opciones de `registerOptions()`. */
  register: (credential: WebAuthnCredentialJSON, name?: string) => Promise<PasskeySummary>;
  /**
   * Las opciones para `navigator.credentials.get()`. Sin `email`, el flujo es con credenciales
   * descubribles. Con `email`, no revela si la cuenta existe: un mail desconocido trae la misma
   * forma que el flujo sin mail.
   */
  authenticateOptions: (email?: string) => Promise<WebAuthnJSON>;
  /** Verifica la aserción y completa el login — persiste tokens, hidrata la sesión. */
  authenticate: (credential: WebAuthnCredentialJSON) => Promise<SignInResult>;
  /** Las credenciales registradas del actor de la sesión actual. */
  list: () => Promise<PasskeySummary[]>;
  /** Borra una credencial del actor de la sesión actual. */
  remove: (passkeyId: string) => Promise<{ deleted: boolean }>;
}

/**
 * El plugin de passkeys (WebAuthn): transporte HTTP puro, sin tocar `navigator.credentials`.
 *
 * Para el flujo completo en el navegador (codificar/decodificar las opciones, invocar
 * `navigator.credentials`), usá el subpath `@hexcore-js/darwin-client/webauthn`, que envuelve este
 * plugin — este plugin también sirve solo para una app que ya arma esas llamadas a mano.
 */
export function passkey() {
  return definePlugin({
    id: "passkey",
    setup: (ctx): PasskeyApi => ({
      registerOptions: () =>
        ctx.$fetch<WebAuthnJSON>("/auth/passkey/register/options", { method: "POST" }),
      register: (credential, name) =>
        ctx
          .$fetch<PasskeySummaryDelServidor>("/auth/passkey/register", {
            method: "POST",
            body: { credential, name },
          })
          .then(aResumen),
      authenticateOptions: (email) =>
        ctx.$fetch<WebAuthnJSON>("/auth/passkey/authenticate/options", {
          method: "POST",
          body: { email },
        }),
      authenticate: async (credential) => {
        const session = await ctx.$fetch<SessionResponse>("/auth/passkey/authenticate", {
          method: "POST",
          body: { credential },
        });
        return ctx.completeAuthentication(session);
      },
      list: () =>
        ctx
          .$fetch<PasskeySummaryDelServidor[]>("/auth/passkey")
          .then((lista) => lista.map(aResumen)),
      remove: (passkeyId) =>
        ctx.$fetch<{ deleted: boolean }>(`/auth/passkey/${encodeURIComponent(passkeyId)}`, {
          method: "DELETE",
        }),
    }),
  });
}
