/**
 * Un store externo mínimo, con el contrato de React (`useSyncExternalStore`) y no el de
 * Svelte: no se pueden satisfacer los dos con una función. Svelte exige que `subscribe(run)`
 * llame a `run(valor)` **inmediatamente**; React exige un listener sin argumentos que no se
 * dispare al suscribirse. Se elige React porque es el más restrictivo y el que rompe feo
 * —tearing, renders infinitos— cuando se hace mal. Svelte y Vue se cubren con adaptadores de
 * unas pocas líneas en el subpath `/store` (sub-fase posterior).
 *
 * `getSnapshot()` es referencialmente estable entre llamadas mientras el estado no cambie:
 * devuelve la misma referencia hasta el próximo `setState`, que es lo que evita que
 * `useSyncExternalStore` entre en un loop de renders creyendo que el snapshot cambió en cada
 * lectura.
 */
export interface Store<T> {
  getSnapshot: () => T;
  subscribe: (listener: () => void) => () => void;
  setState: (updater: T | ((previous: T) => T)) => void;
}

export function createStore<T>(initial: T): Store<T> {
  let estado = initial;
  const listeners = new Set<() => void>();

  function getSnapshot(): T {
    return estado;
  }

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }

  function setState(updater: T | ((previous: T) => T)): void {
    const siguiente =
      typeof updater === "function" ? (updater as (previous: T) => T)(estado) : updater;
    if (siguiente === estado) return;
    estado = siguiente;
    for (const listener of listeners) {
      listener();
    }
  }

  return { getSnapshot, subscribe, setState };
}
