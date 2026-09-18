/**
 * `@hexcore-js/darwin-client` — cliente TypeScript para Darwin, el módulo de identidad de
 * HexCore. Agnóstico de framework de UI, de runtime y de backend.
 *
 * `createDarwinClient()` expone el núcleo (`$fetch` tipado, refresh single-flight, store de
 * sesión, `signIn`/`signOut`/`refresh`/`me`) y un sistema de plugins con registro explícito:
 * `createDarwinClient({ ..., plugins: [twoFactor()] })` agrega `client.twoFactor` con
 * inferencia completa. Los subpaths del navegador (`/webauthn`, `/store`) llegan en una
 * sub-fase posterior.
 */

export type {
  And,
  Condition,
  ConditionEvaluator,
  ConditionResult,
  Const,
  ConstScalar,
  Contains,
  Eq,
  EvaluationContext,
  Gt,
  Gte,
  In,
  Lt,
  Lte,
  Ne,
  Not,
  Or,
  Predicate,
  StartsWith,
  TimeBetween,
  Value,
  Var,
  WithinScope,
} from "./authz/conditions";
export { compileCondition, evaluateCondition } from "./authz/conditions";
export type { CompiledPermissionSet } from "./authz/matcher";
export { compilePermissions, grantsPermission } from "./authz/matcher";
export type {
  AccessControl,
  AccessControlDef,
  ActionOf,
  Permission,
  ResourceOf,
  Schema,
} from "./authz/schema";
export { defineAccessControl } from "./authz/schema";
export type {
  PermissionSnapshotState,
  PermissionSnapshotStatus,
  PermissionStore,
  PermissionStoreOptions,
} from "./authz/store";
export { createPermissionStore } from "./authz/store";
export type { DarwinClient, DarwinClientOptionsWithPlugins } from "./core/client";
export { createDarwinClient } from "./core/client";
export type { DarwinCode, DarwinErrorParams, ParsedWwwAuthenticate } from "./core/errors";
export {
  DarwinError,
  darwinErrorFromResponse,
  isAccessDenied,
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
  SignInRequest,
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
  DrbacApi,
  DrbacCheckOptions,
  DrbacEvaluation,
  DrbacOptions,
  DrbacSnapshotState,
  DrbacSnapshotStatus,
} from "./plugins/drbac";
export { drbac } from "./plugins/drbac";
export type { ImpersonateApi, ImpersonationStatus } from "./plugins/impersonate";
export { impersonate } from "./plugins/impersonate";
export type { MagicLinkApi, MagicLinkRequested } from "./plugins/magic-link";
export { magicLink } from "./plugins/magic-link";
export type {
  LinkedProviders,
  OAuthApi,
  OAuthAuthorization,
  OAuthCallbackParams,
  OAuthCallbackResult,
} from "./plugins/oauth";
export { oauth } from "./plugins/oauth";
export type {
  Invitation,
  InvitationIssued,
  Member,
  Organization,
  OrganizationApi,
  OrgRole,
} from "./plugins/organization";
export { organization } from "./plugins/organization";
export type {
  PasskeyApi,
  PasskeySummary,
  WebAuthnCredentialJSON,
  WebAuthnJSON,
} from "./plugins/passkey";
export { passkey } from "./plugins/passkey";
export type { RbacApi, RbacCheckOptions, RbacOptions } from "./plugins/rbac";
export { rbac } from "./plugins/rbac";
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
