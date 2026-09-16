/**
 * `@hexcore/darwin-client` — cliente TypeScript para Darwin, el módulo de identidad de
 * HexCore. Agnóstico de framework de UI, de runtime y de backend.
 *
 * `createDarwinClient()` expone el núcleo (`$fetch` tipado, refresh single-flight, store de
 * sesión, `signIn`/`signOut`/`refresh`/`me`) y un sistema de plugins con registro explícito:
 * `createDarwinClient({ ..., plugins: [twoFactor()] })` agrega `client.twoFactor` con
 * inferencia completa. Los subpaths del navegador (`/webauthn`, `/store`) llegan en una
 * sub-fase posterior.
 */

export type { DarwinClient, DarwinClientOptionsWithPlugins } from "./core/client";
export { createDarwinClient } from "./core/client";
export type { DarwinCode, DarwinErrorParams, ParsedWwwAuthenticate } from "./core/errors";
export {
  DarwinError,
  darwinErrorFromResponse,
  isRefreshable,
  isSessionDead,
  isTwoFactorRequired,
  parseWwwAuthenticate,
} from "./core/errors";
export type { RequestOptions } from "./core/fetcher";
export type {
  DarwinClientOptions,
  MeResponse,
  SessionResponse,
  SessionState,
  SignInResult,
} from "./core/types";
export type { DarwinErrorCode } from "./generated/error-codes";
export { ERROR_CODES } from "./generated/error-codes";
export type {
  DarwinClientPlugin,
  DarwinClientPluginContext,
  PluginsApi,
} from "./plugins";
export { definePlugin, validatePlugins } from "./plugins";
export type {
  TwoFactorApi,
  TwoFactorEnrollment,
  TwoFactorStatus,
} from "./plugins/two-factor";
export { twoFactor } from "./plugins/two-factor";
export type {
  AsyncStorageLike,
  BearerTransportOptions,
  CookieJar,
  CookieTransportOptions,
  DarwinTokens,
  DarwinTransportName,
  TokenStorage,
  Transport,
} from "./transport";
export {
  BearerTransport,
  CookieTransport,
  cookieStoreJar,
  documentCookieJar,
  fromAsyncStorage,
  localStorageAdapter,
  memoryCookieJar,
  memoryStorage,
} from "./transport";
