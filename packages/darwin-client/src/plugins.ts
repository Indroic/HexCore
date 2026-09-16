import type { RequestOptions } from "./core/fetcher";
import type { SessionResponse, SessionState, SignInResult } from "./core/types";

/**
 * Lo que un plugin recibe para construir su API. No es el `DarwinClient` entero: un plugin no
 * tiene por qué poder invocar a otro plugin ni reconstruir el cliente — sólo necesita pegarle
 * al backend y participar del ciclo de vida de la sesión.
 */
export interface DarwinClientPluginContext {
  $fetch: <T>(path: string, init?: RequestOptions) => Promise<T>;
  session: {
    subscribe: (listener: () => void) => () => void;
    getSnapshot: () => SessionState;
    getServerSnapshot: () => SessionState;
  };
  refresh: () => Promise<void>;
  /**
   * Termina un login que ya tiene un `SessionResponse` en mano: persiste los tokens en el
   * transporte, arranca a trackear el vencimiento del access para el refresh proactivo, marca
   * la sesión viva (por si un refresh anterior la había marcado muerta) e hidrata el store.
   *
   * Existe para que `two_factor.complete()`, un callback de `oauth`, o `passkey.authenticate()`
   * no tengan que reimplementar esa secuencia de cuatro pasos cada uno — es exactamente lo que
   * `signIn()` del núcleo hace en su camino feliz, y es el mismo camino para cualquier ruta que
   * abra una sesión.
   */
  completeAuthentication: (session: SessionResponse) => Promise<SignInResult>;
}

/** Un plugin de `@hexcore-js/darwin-client`. `id` es la clave con la que cuelga de `client.<id>`. */
export interface DarwinClientPlugin<Id extends string = string, Api = unknown> {
  readonly id: Id;
  /**
   * Otros plugins que tienen que estar registrados **antes** que éste. El registro valida que
   * existan y que no formen un ciclo — al construir el cliente, no en el primer request: un
   * plugin mal cableado tiene que fallar ruidosamente en el arranque, no en producción la
   * primera vez que alguien lo usa.
   */
  readonly requires?: readonly string[];
  setup: (ctx: DarwinClientPluginContext) => Api;
}

/**
 * Envuelve un plugin de terceros para que tipe igual que los de acá — sin esto, TypeScript no
 * puede inferir `Id` y `Api` desde un objeto literal y `client.miPlugin` termina en `any`.
 *
 * Uso::
 *
 *     const miPlugin = definePlugin({
 *       id: "miPlugin",
 *       setup: (ctx) => ({
 *         async algo() { return ctx.$fetch("/mi-ruta"); },
 *       }),
 *     });
 */
export function definePlugin<const Id extends string, Api>(
  plugin: DarwinClientPlugin<Id, Api>,
): DarwinClientPlugin<Id, Api> {
  return plugin;
}

// ── El truco de tipos: preservar la tupla e intersectar sus APIs ────────────────
//
// Sin `const TPlugins` en `createDarwinClient` (ver core/client.ts), `plugins: [twoFactor(),
// organization()]` se ensancha a `DarwinClientPlugin[]` y `TPlugins[number]` colapsa a la
// **unión** de los dos — `client.twoFactor` deja de existir porque TypeScript no puede indexar
// una unión por una clave que sólo un miembro tiene. Con `const`, la tupla se preserva literal.

type UnionToIntersection<U> = (U extends unknown ? (k: U) => void : never) extends (
  k: infer I,
) => void
  ? I
  : never;

type ApiDePlugin<P> =
  P extends DarwinClientPlugin<infer Id, infer Api> ? { [K in Id]: Api } : never;

/** Aplana una intersección para que el hover del editor muestre el objeto y no `A & B & C`. */
type Pretty<T> = { [K in keyof T]: T[K] } & {};

/** El objeto `{ [id]: Api }` combinado de todos los plugins de la tupla. */
export type PluginsApi<TPlugins extends readonly DarwinClientPlugin[]> = Pretty<
  UnionToIntersection<ApiDePlugin<TPlugins[number]>>
>;

/**
 * Valida el registro: sin nombres duplicados, sin un `requires` que apunte a un plugin no
 * registrado, sin ciclos. Corre al construir el cliente — un error acá tiene que aparecer en
 * el arranque de la app, con el nombre del plugin culpable, no como un `undefined` tres capas
 * abajo la primera vez que alguien invoca `client.algúnPlugin`.
 */
export function validatePlugins(plugins: readonly DarwinClientPlugin[]): void {
  const nombres = new Set<string>();
  for (const plugin of plugins) {
    if (nombres.has(plugin.id)) {
      throw new Error(
        `Two plugins registered with the same id "${plugin.id}". Ids have to be unique: ` +
          "the id is the key each plugin hangs off in `client.<id>`.",
      );
    }
    nombres.add(plugin.id);
  }

  for (const plugin of plugins) {
    for (const requerido of plugin.requires ?? []) {
      if (!nombres.has(requerido)) {
        throw new Error(
          `Plugin "${plugin.id}" requires "${requerido}", which is not in the plugin list ` +
            `passed to createDarwinClient(). Registered plugins: ` +
            `${[...nombres].join(", ") || "(none)"}.`,
        );
      }
    }
  }

  const porId = new Map(plugins.map((plugin) => [plugin.id, plugin]));
  const enLaPilaActual = new Set<string>();
  const yaVisitado = new Set<string>();

  function visitar(id: string, cadena: readonly string[]): void {
    if (yaVisitado.has(id)) return;
    if (enLaPilaActual.has(id)) {
      throw new Error(`Dependency cycle between plugins: ${[...cadena, id].join(" -> ")}.`);
    }

    enLaPilaActual.add(id);
    const requiereDe = porId.get(id)?.requires ?? [];
    for (const dependencia of requiereDe) {
      visitar(dependencia, [...cadena, id]);
    }
    enLaPilaActual.delete(id);
    yaVisitado.add(id);
  }

  for (const plugin of plugins) {
    visitar(plugin.id, []);
  }
}
