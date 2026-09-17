/**
 * `@hexcore-js/darwin-client/store` — adaptadores del store de sesión a otros contratos de store
 * que no son el de React.
 *
 * `client.session` (`subscribe`/`getSnapshot`/`getServerSnapshot`) ya es exactamente lo que
 * `useSyncExternalStore` de React pide, así que React no necesita nada de este subpath:
 *
 *     useSyncExternalStore(client.session.subscribe, client.session.getSnapshot, client.session.getServerSnapshot)
 *
 * Un store de Svelte es distinto: `subscribe(run)` tiene que invocar `run` **inmediatamente**
 * con el valor actual (no sólo tras el próximo cambio), y de nuevo cada vez que cambia — es lo
 * que permite escribir `$session` en un componente sin un `onMount` previo. Este subpath es ese
 * puente.
 */
import type { Schema } from "./authz/schema";
import type { SessionState } from "./core/types";
import type { RbacApi } from "./plugins/rbac";

export interface SessionLike {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => SessionState;
}

/** Lo que un store de Svelte expone: sólo `subscribe`, con la semántica de invocación inmediata. */
export interface SvelteReadable<T> {
  subscribe: (run: (value: T) => void) => () => void;
}

/**
 * Envuelve `client.session` (o cualquier objeto con la misma forma) como un store de Svelte.
 *
 * Uso::
 *
 *     import { toSvelteStore } from "@hexcore-js/darwin-client/store";
 *
 *     export const session = toSvelteStore(client.session);
 *     // en un componente: `$session.status`
 */
export function toSvelteStore(session: SessionLike): SvelteReadable<SessionState> {
  return {
    subscribe(run) {
      run(session.getSnapshot());
      return session.subscribe(() => run(session.getSnapshot()));
    },
  };
}

/**
 * Guardas agnósticas de framework sobre `client.rbac`: `client.rbac.can(...)` ya es una función
 * síncrona común y silvestre (no hace falta React para usarla), pero un router, un loader de
 * SSR o un middleware de servidor no siempre tienen el `RbacApi` completo a mano — sólo algo
 * con la misma forma de `can`. `createCan`/`createGuard` preservan la inferencia de tipos de
 * `RbacApi<S, Roles>` (recurso/acción quedan acotados al `Schema` declarado) sin importar el
 * resto del plugin.
 */
export type CanFn<S extends Schema, Roles extends string> = RbacApi<S, Roles>["can"];

/** Extrae `can` de `client.rbac`, ya con la misma inferencia de `resource`/`action` del schema. */
export function createCan<S extends Schema, Roles extends string>(
  rbac: Pick<RbacApi<S, Roles>, "can">,
): CanFn<S, Roles> {
  return rbac.can;
}

/** Se lanza cuando falla una guarda armada con `createGuard`. */
export class AccessDeniedGuardError extends Error {
  constructor(
    readonly resource: string,
    readonly action: string,
  ) {
    super(`Access denied: ${resource}.${action}`);
    this.name = "AccessDeniedGuardError";
  }
}

/**
 * Como `createCan`, pero en vez de devolver `boolean` lanza `AccessDeniedGuardError` — para un
 * loader de ruta o un middleware que corta la ejecución en la primera denegación en vez de
 * ramificar sobre un booleano.
 *
 * Uso::
 *
 *     const guard = createGuard(client.rbac);
 *     guard("invoice", "approve"); // lanza si no está permitido; si no, sigue de largo
 */
export function createGuard<S extends Schema, Roles extends string>(
  rbac: Pick<RbacApi<S, Roles>, "can">,
): (...args: Parameters<CanFn<S, Roles>>) => void {
  return (resource, action, options) => {
    if (!rbac.can(resource, action, options)) {
      throw new AccessDeniedGuardError(String(resource), String(action));
    }
  };
}
