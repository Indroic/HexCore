import { DarwinError, isTwoFactorRequired } from "../core/errors";
import type { RequestOptions } from "../core/fetcher";
import { createFetcher } from "../core/fetcher";
import type {
  DarwinClientOptions,
  MeResponse,
  SessionResponse,
  SessionState,
  SignInResult,
} from "../core/types";
import type { DarwinTokens } from "../transport";
import { createRefreshController } from "./refresh";
import { createStore } from "./store";

/**
 * Cuánto antes del vencimiento del access se considera "hay que refrescar". Con 120 s de TTL,
 * 5 s de margen es suficiente para que un request que arranca justo antes del vencimiento no
 * termine de resolver del otro lado con el token ya vencido.
 */
const MARGEN_DE_SEGURIDAD_MS = 5_000;

export interface Session {
  $fetch: <T>(path: string, init?: RequestOptions) => Promise<T>;
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => SessionState;
  getServerSnapshot: () => SessionState;
  signIn: (email: string, password: string) => Promise<SignInResult>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  me: () => Promise<MeResponse>;
  /**
   * Persiste tokens, trackea el vencimiento, marca la sesión viva e hidrata — el camino feliz
   * de `signIn()`, extraído para que un plugin que abre sesión por otra vía (el canje de 2FA,
   * un callback de OAuth, `passkey.authenticate()`) no lo reimplemente cada uno por su cuenta.
   */
  completeAuthentication: (session: SessionResponse) => Promise<SignInResult>;
}

export function createSession(options: DarwinClientOptions): Session {
  const fetchImpl = options.fetch ?? fetch;
  const hydrateOnCreate = options.hydrateOnCreate ?? true;

  // Con `hydrateOnCreate: false` (SSR) el inicial es "unauthenticated" y no "loading": un
  // "loading" eterno del lado del servidor es un spinner que queda renderizado en el HTML,
  // porque el servidor nunca llega a resolver la hidratación.
  const store = createStore<SessionState>(
    hydrateOnCreate ? { status: "loading" } : { status: "unauthenticated" },
  );

  let accessExpiresAt: number | null = null;
  let meEnVuelo: Promise<MeResponse> | null = null;

  function shouldRefreshProactively(): boolean {
    return (
      accessExpiresAt !== null && Date.now() >= accessExpiresAt - MARGEN_DE_SEGURIDAD_MS
    );
  }

  function trackExpiry(session: SessionResponse): void {
    accessExpiresAt = Date.now() + session.expires_in * 1000;
  }

  function tokensDe(session: SessionResponse): DarwinTokens {
    return {
      sessionId: session.session_id,
      expiresIn: session.expires_in,
      tokenType: session.token_type,
      accessToken: session.access_token ?? undefined,
      refreshToken: session.refresh_token ?? undefined,
    };
  }

  const fetcher = createFetcher({
    baseUrl: options.baseUrl,
    transport: options.transport,
    fetchImpl,
    shouldRefreshProactively,
    refresh: () => refreshController.refresh(),
  });

  const refreshController = createRefreshController({
    doRefresh: () =>
      fetcher.$fetch<SessionResponse>("/auth/refresh", {
        method: "POST",
        // Sin estos dos, un refresh recursaría contra sí mismo: se evaluaría a sí mismo como
        // "hay que refrescar" antes de correr, y reintentaría contra su propio 401.
        skipProactiveRefresh: true,
        skipReactiveRetry: true,
      }),
    onSuccess: async (session) => {
      trackExpiry(session);
      await options.transport.persist(tokensDe(session));
    },
    onFailure: (error) => {
      accessExpiresAt = null;
      const razon =
        error instanceof DarwinError && error.code === "TokenRevokedError"
          ? "revoked"
          : "refresh-failed";
      store.setState({ status: "unauthenticated", reason: razon });
    },
  });

  async function completeAuthentication(session: SessionResponse): Promise<SignInResult> {
    trackExpiry(session);
    await options.transport.persist(tokensDe(session));
    refreshController.markAlive();
    await hidratar();
    return { status: "signed-in", session };
  }

  async function signIn(email: string, password: string): Promise<SignInResult> {
    try {
      const session = await fetcher.$fetch<SessionResponse>("/auth/sign-in", {
        method: "POST",
        body: { email, password },
      });
      return await completeAuthentication(session);
    } catch (error) {
      if (isTwoFactorRequired(error)) {
        return { status: "two-factor-required", challenge: error.payload.challenge };
      }
      throw error;
    }
  }

  async function signOut(): Promise<void> {
    try {
      await fetcher.$fetch("/auth/sign-out", { method: "POST" });
    } finally {
      accessExpiresAt = null;
      await options.transport.clear();
      store.setState({ status: "unauthenticated", reason: "signed-out" });
    }
  }

  function me(): Promise<MeResponse> {
    // Deduplicado con la misma mecánica que el refresh: dos componentes que montan a la vez y
    // piden `/auth/me` cada uno no tienen por qué disparar dos requests.
    if (meEnVuelo) return meEnVuelo;

    meEnVuelo = fetcher.$fetch<MeResponse>("/auth/me").finally(() => {
      meEnVuelo = null;
    });
    return meEnVuelo;
  }

  async function hidratar(): Promise<void> {
    try {
      const meResponse = await me();
      store.setState({ status: "authenticated", me: meResponse });
    } catch (error) {
      if (error instanceof DarwinError && error.status === 401) {
        // No pisa una razón más específica (`refresh-failed`/`revoked`) que ya haya puesto
        // `onFailure` del refresh — sólo el estado inicial "sin sesión" cae acá.
        if (store.getSnapshot().status === "loading") {
          store.setState({ status: "unauthenticated" });
        }
        return;
      }
      throw error;
    }
  }

  if (hydrateOnCreate) {
    void hidratar();
  }

  return {
    $fetch: fetcher.$fetch,
    subscribe: store.subscribe,
    getSnapshot: store.getSnapshot,
    // Nunca toca `document`/`window`: es sólo el valor por defecto de un render del servidor.
    getServerSnapshot: () => ({ status: "unauthenticated" }),
    signIn,
    signOut,
    refresh: () => refreshController.refresh(),
    me,
    completeAuthentication,
  };
}
