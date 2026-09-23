import { DarwinError, isDefinitiveAuthFailure, isTwoFactorRequired } from "../core/errors";
import type { RequestOptions } from "../core/fetcher";
import { createFetcher } from "../core/fetcher";
import type {
  DarwinClientOptions,
  MeResponse,
  SessionResponse,
  SessionState,
  SignInRequest,
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

/**
 * Cuánto esperar antes de reintentar un refresh de fondo que falló de forma transitoria (red,
 * proxy roto). Corto a propósito: el refresh token sigue siendo válido, así que no hay razón
 * para esperar a que la app dispare otro request — sólo dar tiempo a que la red se recupere.
 */
const REINTENTO_CORTO_MS = 5_000;

export interface Session {
  $fetch: <T>(path: string, init?: RequestOptions) => Promise<T>;
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => SessionState;
  getServerSnapshot: () => SessionState;
  /**
   * Abre sesión con el identificador que el backend habilite: el mail o el nombre de usuario.
   *
   * El parámetro se llamaba `email` hasta la 0.2: es posicional, así que el renombre no rompe
   * ningún call site, sólo describe mejor lo que ya se podía pasar.
   */
  signIn: (identifier: string, password: string) => Promise<SignInResult>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  me: () => Promise<MeResponse>;
  /**
   * Persiste tokens, trackea el vencimiento, marca la sesión viva e hidrata — el camino feliz
   * de `signIn()`, extraído para que un plugin que abre sesión por otra vía (el canje de 2FA,
   * un callback de OAuth, `passkey.authenticate()`) no lo reimplemente cada uno por su cuenta.
   */
  completeAuthentication: (session: SessionResponse) => Promise<SignInResult>;
  /**
   * Da de baja el timer de refresh en segundo plano y los listeners de `visibilitychange`/
   * `online`. Sin esto, una app que crea y destruye clientes (hot reload, tests, un SPA que
   * desmonta el provider) deja timers/listeners huérfanos corriendo contra un store que ya
   * nadie observa.
   */
  dispose: () => void;
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
  let ttlTimer: ReturnType<typeof setTimeout> | null = null;

  function shouldRefreshProactively(): boolean {
    return (
      accessExpiresAt !== null && Date.now() >= accessExpiresAt - MARGEN_DE_SEGURIDAD_MS
    );
  }

  function detenerRefreshDeFondo(): void {
    if (ttlTimer !== null) {
      clearTimeout(ttlTimer);
      ttlTimer = null;
    }
  }

  /**
   * Programa un refresh que corre solo, sin que la app tenga que hacer ningún `$fetch` —
   * mismo patrón que `programarTtl` en `authz/store.ts`. Es lo que evita que una pestaña
   * inactiva más tiempo que `access_ttl` se quede sin refrescar hasta el próximo request.
   *
   * Si ese refresh falla de forma transitoria, `onFailure` no toca el store (sigue
   * "authenticated"), así que acá se reprograma un reintento corto en vez de esperar a que
   * algo más lo dispare. Un fallo definitivo ya puso el store en "unauthenticated" — nada que
   * reintentar.
   */
  function programarRefreshDeFondo(demoraMs: number): void {
    detenerRefreshDeFondo();
    ttlTimer = setTimeout(() => {
      ttlTimer = null;
      void refreshController.refresh().catch(() => {
        if (store.getSnapshot().status === "authenticated") {
          programarRefreshDeFondo(REINTENTO_CORTO_MS);
        }
      });
    }, demoraMs);
  }

  function trackExpiry(session: SessionResponse): void {
    accessExpiresAt = Date.now() + session.expires_in * 1000;
    programarRefreshDeFondo(
      Math.max(0, session.expires_in * 1000 - MARGEN_DE_SEGURIDAD_MS),
    );
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
      // Fallo transitorio (red, proxy roto): no hubo veredicto del servidor sobre el refresh
      // token, así que no hay motivo para cerrarle la sesión al usuario. El store queda como
      // estaba, y `programarRefreshDeFondo` ya se encarga de reintentar en corto.
      if (!isDefinitiveAuthFailure(error)) return;

      accessExpiresAt = null;
      detenerRefreshDeFondo();
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

  async function signIn(identifier: string, password: string): Promise<SignInResult> {
    try {
      // El cuerpo va tipado contra el contrato generado y no como objeto anónimo: `$fetch`
      // toma `body?: unknown`, así que sin el tipo un campo mal escrito no lo atrapa nadie
      // hasta el 422 del servidor.
      const body: SignInRequest = { identifier, password };
      const session = await fetcher.$fetch<SessionResponse>("/auth/sign-in", {
        method: "POST",
        body,
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
      detenerRefreshDeFondo();
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

  // `visibilitychange`/`online` sólo si el runtime los tiene: en un worker, en SSR, o en React
  // Native no existen, y no son un requisito — son el atajo para cuando el navegador throttleó
  // o pausó `ttlTimer` por tener la pestaña oculta (los timers de fondo no son confiables por
  // sí solos ahí). Mismo patrón que `authz/store.ts`.
  let quitarVisibilidad: (() => void) | undefined;
  let quitarOnline: (() => void) | undefined;

  if (typeof document !== "undefined") {
    const alCambiarVisibilidad = () => {
      if (document.visibilityState !== "visible") return;
      if (shouldRefreshProactively()) void refreshController.refresh().catch(() => {});
    };
    document.addEventListener("visibilitychange", alCambiarVisibilidad);
    quitarVisibilidad = () =>
      document.removeEventListener("visibilitychange", alCambiarVisibilidad);
  }

  if (typeof window !== "undefined") {
    const alVolverOnline = () => {
      if (shouldRefreshProactively()) void refreshController.refresh().catch(() => {});
    };
    window.addEventListener("online", alVolverOnline);
    quitarOnline = () => window.removeEventListener("online", alVolverOnline);
  }

  function dispose(): void {
    detenerRefreshDeFondo();
    quitarVisibilidad?.();
    quitarOnline?.();
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
    dispose,
  };
}
