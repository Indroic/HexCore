import { afterEach, describe, expect, it, vi } from "vitest";
import type { PasskeyApi } from "../../src/plugins/passkey";
import {
  authenticateWithPasskey,
  isWebAuthnSupported,
  registerPasskey,
  WebAuthnUnavailableError,
} from "../../src/webauthn";

function fakePasskeyApi(): PasskeyApi {
  return {
    registerOptions: vi.fn(async () => ({ challenge: "c1" })),
    register: vi.fn(async (credential) => ({
      id: "p1",
      name: null,
      aaguid: null,
      backedUp: false,
      createdAt: null,
      lastUsedAt: null,
      // Guardamos la credencial recibida para que el test pueda inspeccionarla.
      _credentialRecibida: credential,
      // biome-ignore lint/suspicious/noExplicitAny: forma de test, no del contrato real.
    })) as any,
    authenticateOptions: vi.fn(async () => ({ challenge: "c2" })),
    authenticate: vi.fn(async () => ({ status: "signed-in", session: {} }) as never),
    list: vi.fn(async () => []),
    remove: vi.fn(async () => ({ deleted: true })),
  };
}

describe("isWebAuthnSupported / entorno sin navegador", () => {
  it("da false en Node (sin window/navigator)", () => {
    expect(isWebAuthnSupported()).toBe(false);
  });

  it("registerPasskey() lanza WebAuthnUnavailableError sin navegador", async () => {
    await expect(registerPasskey(fakePasskeyApi())).rejects.toBeInstanceOf(
      WebAuthnUnavailableError,
    );
  });

  it("authenticateWithPasskey() lanza WebAuthnUnavailableError sin navegador", async () => {
    await expect(authenticateWithPasskey(fakePasskeyApi())).rejects.toBeInstanceOf(
      WebAuthnUnavailableError,
    );
  });
});

describe("con un navegador simulado", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubNavegadorConWebAuthn(options: {
    create?: () => Promise<unknown>;
    get?: () => Promise<unknown>;
  }) {
    class FakePublicKeyCredential {
      static parseCreationOptionsFromJSON(json: unknown) {
        return json;
      }
      static parseRequestOptionsFromJSON(json: unknown) {
        return json;
      }
      toJSON() {
        return { id: "cred-fake" };
      }
    }

    vi.stubGlobal("window", {});
    vi.stubGlobal("PublicKeyCredential", FakePublicKeyCredential);
    vi.stubGlobal("navigator", {
      credentials: {
        create: options.create ?? (async () => new FakePublicKeyCredential()),
        get: options.get ?? (async () => new FakePublicKeyCredential()),
      },
    });

    return FakePublicKeyCredential;
  }

  it("isWebAuthnSupported() da true con las conversiones JSON presentes", () => {
    stubNavegadorConWebAuthn({});
    expect(isWebAuthnSupported()).toBe(true);
  });

  it("registerPasskey() pide las opciones, crea la credencial y la guarda", async () => {
    const FakePublicKeyCredential = stubNavegadorConWebAuthn({});
    const passkey = fakePasskeyApi();

    const resumen = await registerPasskey(passkey, "mi laptop");

    expect(passkey.registerOptions).toHaveBeenCalledTimes(1);
    expect(passkey.register).toHaveBeenCalledWith({ id: "cred-fake" }, "mi laptop");
    expect(resumen.id).toBe("p1");
    void FakePublicKeyCredential;
  });

  it("registerPasskey() lanza si create() no devuelve una PublicKeyCredential (cancelado)", async () => {
    stubNavegadorConWebAuthn({ create: async () => null });
    await expect(registerPasskey(fakePasskeyApi())).rejects.toBeInstanceOf(
      WebAuthnUnavailableError,
    );
  });

  it("authenticateWithPasskey() pide las opciones, autentica y completa el login", async () => {
    stubNavegadorConWebAuthn({});
    const passkey = fakePasskeyApi();

    const resultado = await authenticateWithPasskey(passkey, "alguien@test.com");

    expect(passkey.authenticateOptions).toHaveBeenCalledWith("alguien@test.com");
    expect(passkey.authenticate).toHaveBeenCalledWith({ id: "cred-fake" });
    expect(resultado.status).toBe("signed-in");
  });

  it("authenticateWithPasskey() lanza si get() no devuelve una PublicKeyCredential (cancelado)", async () => {
    stubNavegadorConWebAuthn({ get: async () => null });
    await expect(authenticateWithPasskey(fakePasskeyApi())).rejects.toBeInstanceOf(
      WebAuthnUnavailableError,
    );
  });
});
