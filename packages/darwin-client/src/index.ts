/**
 * `@hexcore/darwin-client` — cliente TypeScript para Darwin, el módulo de identidad de
 * HexCore. Agnóstico de framework de UI, de runtime y de backend.
 *
 * Este archivo es el esqueleto de la Fase 2 del plan de monorepo: por ahora exporta la capa
 * de transporte (cookie/Bearer) y los códigos de error generados desde el contrato Python.
 * `createDarwinClient()` — el fetcher con refresh single-flight y el store de sesión — llega
 * en la próxima sub-fase.
 */

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
