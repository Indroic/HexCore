import type { SessionResponse, SignInResult } from "../core/types";
import { definePlugin } from "../plugins";

export interface LinkedProviders {
  providers: string[];
}

export interface OAuthAuthorization {
  /** A dónde mandar al navegador para que el usuario autorice con el proveedor. */
  url: string;
  /** El valor de un solo uso que el callback tiene que devolver intacto. */
  state: string;
}

export interface OAuthCallbackParams {
  code: string;
  state: string;
  /** El mismo `redirectUri` que se usó para `start()` — el proveedor lo exige idéntico. */
  redirectUri: string;
}

/**
 * El resultado de `handleCallback()`. `result` es el mismo `SignInResult` que devuelve
 * `signIn()` — nunca "two-factor-required" acá, porque OAuth no tiene ese segundo paso — y
 * `created` distingue un signup implícito de un login: es lo que decide si mandar al usuario
 * nuevo a onboarding en vez de al home.
 */
export interface OAuthCallbackResult {
  result: SignInResult;
  created: boolean;
}

export interface OAuthApi {
  /** Los proveedores configurados en el backend — lo que la interfaz necesita para dibujar botones. */
  providers: () => Promise<LinkedProviders>;
  /** Inicia el flujo de login: la app redirige al navegador a `authorization.url`. */
  start: (provider: string, redirectUri: string) => Promise<OAuthAuthorization>;
  /**
   * Canjea lo que el navegador recibió del callback del proveedor y completa el login: persiste
   * los tokens, hidrata la sesión, y el store queda `authenticated`.
   */
  handleCallback: (
    provider: string,
    params: OAuthCallbackParams,
  ) => Promise<OAuthCallbackResult>;
  /** Inicia una vinculación de `provider` a la cuenta ya autenticada de la sesión actual. */
  link: (provider: string, redirectUri: string) => Promise<OAuthAuthorization>;
  /** Los proveedores vinculados al actor de la sesión actual. */
  linked: () => Promise<LinkedProviders>;
  /** Desvincula `provider` de la cuenta de la sesión actual. */
  unlink: (provider: string) => Promise<{ unlinked: boolean }>;
}

/**
 * El plugin de OAuth: login/vinculación con un proveedor externo (Google, GitHub, ...).
 *
 * Uso::
 *
 *     const client = createDarwinClient({ baseUrl, transport, plugins: [oauth()] });
 *
 *     const { url } = await client.oauth.start("google", redirectUri);
 *     window.location.assign(url);
 *     // ...el proveedor vuelve a `redirectUri` con `?code=...&state=...`...
 *     const { result, created } = await client.oauth.handleCallback("google", {
 *       code, state, redirectUri,
 *     });
 */
export function oauth() {
  return definePlugin({
    id: "oauth",
    setup: (ctx): OAuthApi => ({
      providers: () => ctx.$fetch<LinkedProviders>("/auth/oauth/providers"),
      start: (provider, redirectUri) =>
        ctx.$fetch<OAuthAuthorization>(
          `/auth/oauth/${encodeURIComponent(provider)}/start?redirect_uri=${encodeURIComponent(redirectUri)}`,
        ),
      handleCallback: async (provider, { code, state, redirectUri }) => {
        const query = new URLSearchParams({ code, state, redirect_uri: redirectUri });
        const { created, ...session } = await ctx.$fetch<
          SessionResponse & { created: boolean }
        >(`/auth/oauth/${encodeURIComponent(provider)}/callback?${query}`);
        const result = await ctx.completeAuthentication(session);
        return { result, created };
      },
      link: (provider, redirectUri) =>
        ctx.$fetch<OAuthAuthorization>(
          `/auth/oauth/${encodeURIComponent(provider)}/link?redirect_uri=${encodeURIComponent(redirectUri)}`,
        ),
      linked: () => ctx.$fetch<LinkedProviders>("/auth/oauth/linked"),
      unlink: (provider) =>
        ctx.$fetch<{ unlinked: boolean }>(`/auth/oauth/${encodeURIComponent(provider)}`, {
          method: "DELETE",
        }),
    }),
  });
}
