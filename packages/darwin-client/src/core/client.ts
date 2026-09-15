import type { Session } from "../session/session";
import { createSession } from "../session/session";
import type { RequestOptions } from "./fetcher";
import type { DarwinClientOptions, MeResponse, SessionState, SignInResult } from "./types";

/**
 * El cliente. Por ahora expone el núcleo (`$fetch`, sesión, auth por contraseña) sin sistema
 * de plugins — eso llega en la sub-fase siguiente, que envuelve esto para agregar
 * `client.twoFactor`, `client.oauth`, etc. sin tocar nada de lo que hay acá.
 */
export interface DarwinClient {
  /**
   * El `fetch` tipado crudo, para pegarle a cualquier ruta del contrato que el núcleo o un
   * plugin todavía no envuelvan. Ya trae el transporte, el refresh proactivo/reactivo y el
   * parseo de errores — no hay que repetir nada de eso.
   */
  $fetch: <T>(path: string, init?: RequestOptions) => Promise<T>;

  session: {
    subscribe: (listener: () => void) => () => void;
    getSnapshot: () => SessionState;
    getServerSnapshot: () => SessionState;
  };

  signIn: (email: string, password: string) => Promise<SignInResult>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  me: () => Promise<MeResponse>;
}

export function createDarwinClient(options: DarwinClientOptions): DarwinClient {
  const session: Session = createSession(options);

  return {
    $fetch: session.$fetch,
    session: {
      subscribe: session.subscribe,
      getSnapshot: session.getSnapshot,
      getServerSnapshot: session.getServerSnapshot,
    },
    signIn: session.signIn,
    signOut: session.signOut,
    refresh: session.refresh,
    me: session.me,
  };
}
