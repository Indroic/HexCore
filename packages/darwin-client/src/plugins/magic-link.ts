import type { SessionResponse, SignInResult } from "../core/types";
import { definePlugin } from "../plugins";

export interface MagicLinkRequested {
  sent: boolean;
  /** Sólo viaja en un despliegue que no manda mails de verdad. En producción queda `undefined`. */
  token?: string;
}

export interface MagicLinkApi {
  /**
   * Pide un magic link. **Responde igual exista o no la cuenta** (mismo shape, `sent: true` en
   * los dos casos) — no uses esta respuesta para inferir si el email tiene cuenta.
   */
  request: (email: string) => Promise<MagicLinkRequested>;
  /**
   * Canjea el link y completa el login — mismo resultado que un `signIn` exitoso: persiste los
   * tokens, hidrata la sesión, y el store queda `authenticated`.
   */
  consume: (email: string, token: string) => Promise<SignInResult>;
}

/**
 * El plugin de magic link: login sin contraseña por un token de un solo uso mandado por mail.
 *
 * Uso::
 *
 *     const client = createDarwinClient({ baseUrl, transport, plugins: [magicLink()] });
 *
 *     await client.magicLink.request(email);
 *     // ...el usuario hace click en el link del mail, que trae `email` y `token`...
 *     await client.magicLink.consume(email, token);
 */
export function magicLink() {
  return definePlugin({
    id: "magicLink",
    setup: (ctx): MagicLinkApi => ({
      request: (email) =>
        ctx.$fetch<MagicLinkRequested>("/auth/magic-link/request", {
          method: "POST",
          body: { email },
        }),
      consume: async (email, token) => {
        const session = await ctx.$fetch<SessionResponse>("/auth/magic-link/consume", {
          method: "POST",
          body: { email, token },
        });
        return ctx.completeAuthentication(session);
      },
    }),
  });
}
