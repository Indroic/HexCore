import type { DarwinClientPlugin, DarwinClientPluginContext, PluginsApi } from "../plugins";
import { validatePlugins } from "../plugins";
import type { Session } from "../session/session";
import { createSession } from "../session/session";
import type { RequestOptions } from "./fetcher";
import type { DarwinClientOptions, MeResponse, SessionState, SignInResult } from "./types";

/** El núcleo: `$fetch`, sesión, y auth por contraseña — sin los plugins registrados. */
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

export interface DarwinClientOptionsWithPlugins<
  TPlugins extends readonly DarwinClientPlugin[] = [],
> extends DarwinClientOptions {
  /**
   * Los plugins, **registrados explícitamente** — nunca por descubrimiento. Un plugin que se
   * activa por estar instalado es un plugin que nadie puede desactivar; acá la lista que
   * pasás es la lista completa, sin sorpresas.
   */
  plugins?: TPlugins;
}

/**
 * `const TPlugins` (TS 5.0+) es lo que hace que `client.twoFactor` exista en el tipo de
 * retorno: sin `const`, `plugins: [twoFactor(), organization()]` se ensancha a
 * `DarwinClientPlugin[]` y `TPlugins[number]` colapsa a la **unión** de los dos plugins en vez
 * de preservar la tupla — y no se puede indexar una unión por una clave que sólo un miembro
 * tiene. Ver el comentario largo en `plugins.ts`.
 */
export function createDarwinClient<
  const TPlugins extends readonly DarwinClientPlugin[] = [],
>(options: DarwinClientOptionsWithPlugins<TPlugins>): DarwinClient & PluginsApi<TPlugins> {
  const session: Session = createSession(options);

  const base: DarwinClient = {
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

  const plugins = options.plugins ?? ([] as unknown as TPlugins);
  // Corre al construir el cliente, no en el primer request que toque un plugin mal cableado:
  // un `requires` que apunta a nada, o un ciclo, tiene que aparecer en el arranque de la app.
  validatePlugins(plugins);

  const ctx: DarwinClientPluginContext = {
    $fetch: base.$fetch,
    session: base.session,
    refresh: base.refresh,
    completeAuthentication: session.completeAuthentication,
  };

  const apiDePlugins: Record<string, unknown> = {};
  for (const plugin of plugins) {
    apiDePlugins[plugin.id] = plugin.setup(ctx);
  }

  return Object.assign(base, apiDePlugins) as DarwinClient & PluginsApi<TPlugins>;
}
