import type { SessionResponse, SignInResult } from "../core/types";
import { definePlugin } from "../plugins";

export interface TwoFactorStatus {
  enrolled: boolean;
  confirmed: boolean;
}

/**
 * ⚠️ Lleva el secreto en claro: es la única vez que sale de la aplicación, porque el usuario
 * tiene que escanearlo. No lo loguees ni lo guardes más allá de mostrar el QR.
 */
export interface TwoFactorEnrollment {
  secret: string;
  uri: string;
  confirmed: boolean;
}

export interface TwoFactorApi {
  /** El estado del segundo factor del actor de la sesión actual. */
  status: () => Promise<TwoFactorStatus>;
  /** Inscribe un factor nuevo, sin activarlo. */
  enroll: () => Promise<TwoFactorEnrollment>;
  /** Activa el factor inscripto. */
  confirm: (code: string) => Promise<{ confirmed: boolean }>;
  /** Desactiva el factor, exigiendo un código válido. */
  disable: (code: string) => Promise<{ disabled: boolean }>;
  /**
   * Canjea el `challenge` que trajo el 401 de `signIn()` (`isTwoFactorRequired(error)` narrowea
   * `error.payload.challenge`) y completa el login — mismo resultado que un `signIn` exitoso:
   * persiste los tokens, hidrata la sesión, y el store queda `authenticated`.
   */
  complete: (challenge: string, code: string) => Promise<SignInResult>;
}

/**
 * El plugin de segundo factor. Cierra el flujo de la Fase 0.1 del lado del cliente: el
 * `challenge` viaja en el cuerpo del 401 (`TwoFactorRequiredError.payload.challenge`, del lado
 * Python) y `complete()` es lo que lo canjea sin que la app tenga que armar el request a mano.
 *
 * Uso::
 *
 *     const client = createDarwinClient({ baseUrl, transport, plugins: [twoFactor()] });
 *
 *     const resultado = await client.signIn(email, password);
 *     if (resultado.status === "two-factor-required") {
 *       const codigo = await pedirCodigoAlUsuario();
 *       await client.twoFactor.complete(resultado.challenge, codigo);
 *     }
 */
export function twoFactor() {
  return definePlugin({
    id: "twoFactor",
    setup: (ctx): TwoFactorApi => ({
      status: () => ctx.$fetch<TwoFactorStatus>("/auth/2fa"),
      enroll: () => ctx.$fetch<TwoFactorEnrollment>("/auth/2fa/enroll", { method: "POST" }),
      confirm: (code) =>
        ctx.$fetch<{ confirmed: boolean }>("/auth/2fa/confirm", {
          method: "POST",
          body: { code },
        }),
      disable: (code) =>
        ctx.$fetch<{ disabled: boolean }>("/auth/2fa/disable", {
          method: "POST",
          body: { code },
        }),
      complete: async (challenge, code) => {
        const session = await ctx.$fetch<SessionResponse>("/auth/2fa/challenge", {
          method: "POST",
          body: { challenge, code },
        });
        return ctx.completeAuthentication(session);
      },
    }),
  });
}
