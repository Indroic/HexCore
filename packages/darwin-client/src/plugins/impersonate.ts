import type { SessionResponse, SignInResult } from "../core/types";
import { definePlugin } from "../plugins";

export interface ImpersonationStatus {
  active: boolean;
  actorId: string | null;
  subjectId: string | null;
  reason: string | null;
  expiresAt: string | null;
}

interface ImpersonationStatusDelServidor {
  active: boolean;
  actor_id: string | null;
  subject_id: string | null;
  reason: string | null;
  expires_at: string | null;
}

export interface ImpersonateApi {
  /**
   * El estado de impersonación de la sesión actual — lo que alimenta la barra de "estás viendo
   * como...".
   */
  status: () => Promise<ImpersonationStatus>;
  /**
   * Empieza a impersonar a `userId`. ⚠️ Con transporte cookie, esto **reemplaza la cookie del
   * operador en el navegador**: es una sesión nueva, no una vista superpuesta. Terminarla exige
   * `stop()` (y, con cookie, un nuevo `signIn()` para volver a ser el operador).
   */
  start: (userId: string, reason: string) => Promise<SignInResult>;
  /**
   * Termina la impersonación activa. No devuelve una sesión: la del operador nunca se tocó — con
   * transporte cookie hay que volver a iniciar sesión, y con transporte bearer basta con seguir
   * usando los tokens que ya tenías guardados de antes de impersonar. En ningún caso este método
   * toca el store de sesión por su cuenta: es la app la que decide qué hacer después (por
   * ejemplo, `client.me()` para refrescar `MeResponse`, o un `signIn()` con cookie).
   */
  stop: () => Promise<{ stopped: boolean }>;
}

function aEstado(s: ImpersonationStatusDelServidor): ImpersonationStatus {
  return {
    active: s.active,
    actorId: s.actor_id,
    subjectId: s.subject_id,
    reason: s.reason,
    expiresAt: s.expires_at,
  };
}

/**
 * El plugin de impersonación: un operador actúa temporalmente como otro usuario, con motivo y
 * vencimiento auditados del lado del servidor.
 */
export function impersonate() {
  return definePlugin({
    id: "impersonate",
    setup: (ctx): ImpersonateApi => ({
      status: () =>
        ctx.$fetch<ImpersonationStatusDelServidor>("/auth/impersonate").then(aEstado),
      start: async (userId, reason) => {
        const session = await ctx.$fetch<SessionResponse>(
          `/auth/impersonate/${encodeURIComponent(userId)}`,
          { method: "POST", body: { reason } },
        );
        return ctx.completeAuthentication(session);
      },
      stop: () =>
        ctx.$fetch<{ stopped: boolean }>("/auth/impersonate/stop", { method: "POST" }),
    }),
  });
}
