import { describe, expect, it } from "vitest";
import { fromAsyncStorage, memoryStorage } from "../../src/transport/storage";

describe("memoryStorage", () => {
  it("guarda, lee y borra", async () => {
    const storage = memoryStorage();

    await storage.set("k", "v");
    expect(await storage.get("k")).toBe("v");

    await storage.remove("k");
    expect(await storage.get("k")).toBeNull();
  });

  it("dos instancias no comparten estado", async () => {
    const a = memoryStorage();
    const b = memoryStorage();

    await a.set("k", "solo-en-a");

    expect(await b.get("k")).toBeNull();
  });
});

describe("fromAsyncStorage", () => {
  it("delega en el storage async subyacente", async () => {
    const respaldo = new Map<string, string>();
    const nativo = {
      getItem: async (key: string) => respaldo.get(key) ?? null,
      setItem: async (key: string, value: string) => {
        respaldo.set(key, value);
      },
      removeItem: async (key: string) => {
        respaldo.delete(key);
      },
    };

    const storage = fromAsyncStorage(nativo);
    await storage.set("k", "v");

    expect(respaldo.get("k")).toBe("v");
    expect(await storage.get("k")).toBe("v");

    await storage.remove("k");
    expect(respaldo.has("k")).toBe(false);
  });
});
