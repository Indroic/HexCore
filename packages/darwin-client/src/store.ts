/**
 * `@hexcore/darwin-client/store` — adaptadores del store de sesión a otros contratos de store
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
import type { SessionState } from "./core/types";

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
 *     import { toSvelteStore } from "@hexcore/darwin-client/store";
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
