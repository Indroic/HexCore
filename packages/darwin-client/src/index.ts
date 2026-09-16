/**
 * `@hexcore/darwin-client` — cliente TypeScript para Darwin, el módulo de identidad de
 * HexCore. Agnóstico de framework de UI, de runtime y de backend.
 *
 * `createDarwinClient()` ya expone el núcleo: `$fetch` tipado con refresh proactivo/reactivo
 * single-flight, el store de sesión (contrato React) y `signIn`/`signOut`/`refresh`/`me`. El
 * sistema de plugins (`client.twoFactor`, `client.oauth`, ...) llega en la próxima sub-fase.
 */

export type { DarwinClient } from "./core/client";
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
