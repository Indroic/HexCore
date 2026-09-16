/**
 * `@hexcore-js/darwin-client/webauthn` — el pegamento con `navigator.credentials` que el plugin
 * `passkey` deja afuera a propósito (ver el docstring de `src/plugins/passkey.ts`). Sólo corre
 * en un navegador: importarlo en Node o React Native tira `WebAuthnUnavailableError` al primer
 * uso, no al importar — un subpath opcional no puede romper un bundle que nunca lo invoca.
 *
 * Usa las conversiones JSON nativas de WebAuthn L3 (`PublicKeyCredential.
 * parseCreationOptionsFromJSON`/`parseRequestOptionsFromJSON` y `credential.toJSON()`) en vez de
 * codificar/decodificar base64url a mano: son parte del spec desde 2023 y evitan mantener una
 * segunda implementación de algo que el navegador ya hace bien.
 */

import type { SignInResult } from "./core/types";
import type { PasskeyApi, PasskeySummary, WebAuthnJSON } from "./plugins/passkey";

export class WebAuthnUnavailableError extends Error {
  constructor(motivo: string) {
    super(`WebAuthn no está disponible acá: ${motivo}`);
    this.name = "WebAuthnUnavailableError";
  }
}

/** Si el runtime actual puede llamar a `registerPasskey`/`authenticateWithPasskey`. */
export function isWebAuthnSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof navigator !== "undefined" &&
    typeof navigator.credentials !== "undefined" &&
    typeof PublicKeyCredential !== "undefined" &&
    typeof PublicKeyCredential.parseCreationOptionsFromJSON === "function" &&
    typeof PublicKeyCredential.parseRequestOptionsFromJSON === "function"
  );
}

function asegurarSoporte(): void {
  if (typeof window === "undefined" || typeof navigator === "undefined") {
    throw new WebAuthnUnavailableError(
      "there is no `window`/`navigator` (are you on Node or in SSR?)",
    );
  }
  if (
    typeof PublicKeyCredential === "undefined" ||
    typeof PublicKeyCredential.parseCreationOptionsFromJSON !== "function" ||
    typeof PublicKeyCredential.parseRequestOptionsFromJSON !== "function"
  ) {
    throw new WebAuthnUnavailableError(
      "this browser does not implement the WebAuthn L3 JSON conversions " +
        "(`PublicKeyCredential.parseCreationOptionsFromJSON`/`parseRequestOptionsFromJSON`)",
    );
  }
}

function comoCredencialDePublicKey(
  credential: Credential | null,
  verbo: string,
): PublicKeyCredential {
  if (!(credential instanceof PublicKeyCredential)) {
    throw new WebAuthnUnavailableError(
      `\`navigator.credentials.${verbo}()\` did not return a PublicKeyCredential (did the ` +
        "user cancel the browser dialog?)",
    );
  }
  return credential;
}

/**
 * Registra una passkey nueva para el actor de la sesión actual: pide las opciones, invoca
 * `navigator.credentials.create()` y guarda la credencial resultante.
 *
 * Uso::
 *
 *     import { registerPasskey } from "@hexcore-js/darwin-client/webauthn";
 *
 *     const resumen = await registerPasskey(client.passkey, "mi laptop");
 */
export async function registerPasskey(
  passkey: PasskeyApi,
  name?: string,
): Promise<PasskeySummary> {
  asegurarSoporte();

  const optionsJSON = await passkey.registerOptions();
  const publicKey = PublicKeyCredential.parseCreationOptionsFromJSON(
    optionsJSON as unknown as PublicKeyCredentialCreationOptionsJSON,
  );
  const credential = comoCredencialDePublicKey(
    await navigator.credentials.create({ publicKey }),
    "create",
  );

  return passkey.register(credential.toJSON() as unknown as Record<string, unknown>, name);
}

/**
 * Autentica con una passkey y completa el login. Sin `email`, el navegador ofrece cualquier
 * credencial descubrible que tenga guardada para este origen.
 *
 * Uso::
 *
 *     import { authenticateWithPasskey } from "@hexcore-js/darwin-client/webauthn";
 *
 *     const resultado = await authenticateWithPasskey(client.passkey, email);
 */
export async function authenticateWithPasskey(
  passkey: PasskeyApi,
  email?: string,
): Promise<SignInResult> {
  asegurarSoporte();

  const optionsJSON: WebAuthnJSON = await passkey.authenticateOptions(email);
  const publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(
    optionsJSON as unknown as PublicKeyCredentialRequestOptionsJSON,
  );
  const credential = comoCredencialDePublicKey(
    await navigator.credentials.get({ publicKey }),
    "get",
  );

  return passkey.authenticate(credential.toJSON() as unknown as Record<string, unknown>);
}
