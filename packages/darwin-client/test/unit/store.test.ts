import { describe, expect, it } from "vitest";
import type { SessionState } from "../../src/core/types";
import { toSvelteStore } from "../../src/store";

function fakeSession(estadoInicial: SessionState) {
  let estado = estadoInicial;
  const listeners = new Set<() => void>();

  return {
    getSnapshot: () => estado,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setState(nuevo: SessionState) {
      estado = nuevo;
      for (const listener of listeners) listener();
    },
  };
}

describe("toSvelteStore", () => {
  it("invoca run inmediatamente con el valor actual al suscribirse", () => {
    const session = fakeSession({ status: "loading" });
    const store = toSvelteStore(session);

    const valores: SessionState[] = [];
    store.subscribe((v) => valores.push(v));

    expect(valores).toEqual([{ status: "loading" }]);
  });

  it("vuelve a invocar run en cada cambio de estado", () => {
    const session = fakeSession({ status: "loading" });
    const store = toSvelteStore(session);

    const valores: SessionState[] = [];
    store.subscribe((v) => valores.push(v));

    session.setState({ status: "unauthenticated" });
    session.setState({
      status: "authenticated",
      me: { actor_id: "u1", subject_id: "u1", impersonating: false },
    });

    expect(valores).toEqual([
      { status: "loading" },
      { status: "unauthenticated" },
      {
        status: "authenticated",
        me: { actor_id: "u1", subject_id: "u1", impersonating: false },
      },
    ]);
  });

  it("el unsubscribe que devuelve corta las notificaciones futuras", () => {
    const session = fakeSession({ status: "loading" });
    const store = toSvelteStore(session);

    const valores: SessionState[] = [];
    const unsubscribe = store.subscribe((v) => valores.push(v));
    unsubscribe();

    session.setState({ status: "unauthenticated" });

    expect(valores).toEqual([{ status: "loading" }]);
  });
});
