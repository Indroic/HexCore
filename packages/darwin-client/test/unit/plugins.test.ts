import { describe, expect, expectTypeOf, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { definePlugin, validatePlugins } from "../../src/plugins";
import type { TwoFactorApi } from "../../src/plugins/two-factor";
import { twoFactor } from "../../src/plugins/two-factor";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";

function plugin(id: string, requires: readonly string[] = []) {
  return definePlugin({ id, requires, setup: () => ({ id }) });
}

describe("validatePlugins", () => {
  it("no lanza con una lista vacía o sin dependencias entre sí", () => {
    expect(() => validatePlugins([])).not.toThrow();
    expect(() => validatePlugins([plugin("a"), plugin("b")])).not.toThrow();
  });

  it("lanza si dos plugins declaran el mismo id", () => {
    expect(() => validatePlugins([plugin("a"), plugin("a")])).toThrow(/same id "a"/);
  });

  it("lanza si un requires apunta a un plugin no registrado", () => {
    expect(() => validatePlugins([plugin("a", ["b"])])).toThrow(/requires "b"/);
  });

  it("no lanza cuando el requires sí está registrado", () => {
    expect(() => validatePlugins([plugin("b"), plugin("a", ["b"])])).not.toThrow();
  });

  it("detecta un ciclo directo (a requiere b, b requiere a)", () => {
    expect(() => validatePlugins([plugin("a", ["b"]), plugin("b", ["a"])])).toThrow(
      /Dependency cycle/,
    );
  });

  it("detecta un ciclo indirecto (a -> b -> c -> a)", () => {
    expect(() =>
      validatePlugins([plugin("a", ["b"]), plugin("b", ["c"]), plugin("c", ["a"])]),
    ).toThrow(/Dependency cycle/);
  });

  it("un diamante (a y b requieren c, d requiere a y b) no es un ciclo", () => {
    expect(() =>
      validatePlugins([
        plugin("c"),
        plugin("a", ["c"]),
        plugin("b", ["c"]),
        plugin("d", ["a", "b"]),
      ]),
    ).not.toThrow();
  });
});

describe("createDarwinClient con plugins — tipos", () => {
  it("client.twoFactor tiene el tipo de TwoFactorApi", () => {
    const client = createDarwinClient({
      baseUrl: "https://api.test",
      transport: new BearerTransport({ storage: memoryStorage() }),
      hydrateOnCreate: false,
      plugins: [twoFactor()],
    });

    expectTypeOf(client.twoFactor).toEqualTypeOf<TwoFactorApi>();
    // El núcleo sigue estando: agregar un plugin no lo tapa.
    expectTypeOf(client.signIn).toBeFunction();
  });

  it("sin plugins, el tipo no tiene ninguna clave de plugin", () => {
    const client = createDarwinClient({
      baseUrl: "https://api.test",
      transport: new BearerTransport({ storage: memoryStorage() }),
      hydrateOnCreate: false,
    });

    // @ts-expect-error — no hay plugins registrados, `twoFactor` no existe en el tipo.
    client.twoFactor;
  });

  it("con dos plugins, los dos quedan tipados a la vez (no colapsan a una unión)", () => {
    const otro = definePlugin({
      id: "otro" as const,
      setup: () => ({ hola: () => "mundo" }),
    });

    const client = createDarwinClient({
      baseUrl: "https://api.test",
      transport: new BearerTransport({ storage: memoryStorage() }),
      hydrateOnCreate: false,
      plugins: [twoFactor(), otro],
    });

    expectTypeOf(client.twoFactor).toEqualTypeOf<TwoFactorApi>();
    expectTypeOf(client.otro.hola).toBeFunction();
  });
});

describe("createDarwinClient con plugins — runtime", () => {
  it("valida el registro al construir, no en el primer request", () => {
    expect(() =>
      createDarwinClient({
        baseUrl: "https://api.test",
        transport: new BearerTransport({ storage: memoryStorage() }),
        hydrateOnCreate: false,
        // `requires` es sólo `string[]`: TypeScript no puede saber estáticamente que
        // "no-existe" no está entre los ids registrados. Eso lo valida `validatePlugins()`
        // en runtime, que es justo lo que este test prueba.
        plugins: [plugin("a", ["no-existe"])],
      }),
    ).toThrow(/requires "no-existe"/);
  });

  it("plugin.setup() recibe un ctx con $fetch, session y completeAuthentication", () => {
    let ctxRecibido: unknown;
    const captador = definePlugin({
      id: "captador",
      setup: (ctx) => {
        ctxRecibido = ctx;
        return {};
      },
    });

    createDarwinClient({
      baseUrl: "https://api.test",
      transport: new BearerTransport({ storage: memoryStorage() }),
      hydrateOnCreate: false,
      plugins: [captador],
    });

    expect(ctxRecibido).toMatchObject({
      $fetch: expect.any(Function),
      refresh: expect.any(Function),
      completeAuthentication: expect.any(Function),
    });
  });
});
