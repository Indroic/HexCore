/**
 * `PermissionStore`: el resumen de permisos de `rbac`, por scope, con revalidación.
 *
 * **Todo lo de acá es optimista para la interfaz.** La autoridad es siempre
 * `AuthorizationEngine.decide()` del lado del servidor, en cada acción real —
 * `/auth/rbac/me/permissions` es un resumen, no una decisión. Este store existe para no
 * esconder ni mostrar de más un botón mientras se espera esa decisión real.
 *
 * **Multi-scope.** `client.rbac.hasRole("accountant", { scope: "org:42" })` tiene que poder
 * responder ya, de forma síncrona, sin awaitear nada — así que el store mantiene un mapa
 * `scope → snapshot` y no un único snapshot. Un scope que nunca se pidió dispara su propio
 * fetch en segundo plano (fire-and-forget) y responde `false`/vacío hasta que resuelva: el
 * mismo criterio "nunca adivines hacia el lado del `allow`" que el resto del módulo.
 *
 * **Revalidación**, en cuatro disparadores:
 *
 * 1. Cambios de sesión (`ctx.session`): sign-in, sign-out, o cualquier transición a
 *    `authenticated` (cubre re-autenticación, impersonación) invalida todos los scopes
 *    cacheados — los permisos de la sesión anterior no tienen por qué seguir siendo los de
 *    la nueva.
 * 2. TTL (`expires_at` de la respuesta): vencido, se revalida sola en segundo plano.
 * 3. `notifyAccessDenied()`: para cuando el consumidor ve un `AccessDeniedError` en **su
 *    propio** código (esto no intercepta `$fetch` del cliente entero — sería meterse en el
 *    camino de peticiones que no tienen nada que ver con `rbac`). Marca el scope activo
 *    "stale" y dispara una revalidación.
 * 4. `visibilitychange`/`online`, sólo si `document`/`window` existen: una pestaña que estuvo
 *    en segundo plano, o que perdió la conexión, revalida sus scopes vencidos al volver.
 *
 * **No hay hook de header de versión** (`X-Darwin-Authz-Version`) en esta fase: el backend de
 * `rbac` (Fase F2) no lo emite todavía en cada response, sólo en el propio cuerpo de
 * `/me/permissions`. Queda para cuando ese mecanismo exista del lado del servidor.
 */
import type { RequestOptions } from "../core/fetcher";
import type { SessionState } from "../core/types";
import {
  type CompiledPermissionSet,
  compilePermissions,
  grantsPermission,
} from "./matcher";

export type PermissionSnapshotStatus = "loading" | "ready" | "stale";

export interface PermissionSnapshotState {
  status: PermissionSnapshotStatus;
  scope: string;
  roles: readonly string[];
  permissions: readonly string[];
  /** `null` mientras no hubo ni una resolución exitosa todavía. */
  version: number | null;
  /** Epoch ms. `null` en el mismo caso que `version`. */
  expiresAt: number | null;
}

interface MePermissionsResponse {
  scope: string;
  roles: string[];
  permissions: string[];
  version: number;
  expires_at: string;
}

function snapshotVacio(
  scope: string,
  status: PermissionSnapshotStatus,
): PermissionSnapshotState {
  return { status, scope, roles: [], permissions: [], version: null, expiresAt: null };
}

interface ScopeEntry {
  state: PermissionSnapshotState;
  compiled: CompiledPermissionSet;
  enVuelo: Promise<void> | null;
  ttlTimer: ReturnType<typeof setTimeout> | null;
}

export interface PermissionStoreOptions {
  $fetch: <T>(path: string, init?: RequestOptions) => Promise<T>;
  session: {
    subscribe: (listener: () => void) => () => void;
    getSnapshot: () => SessionState;
  };
  /** El scope con el que arranca `revalidate()`/`can()` sin `{ scope }` explícito. Global por default. */
  defaultScope?: string;
}

export interface PermissionStore {
  subscribe: (listener: () => void) => () => void;
  /** El snapshot de `scope` (default: el scope por defecto del store). Nunca dispara un fetch. */
  getSnapshot: (scope?: string) => PermissionSnapshotState;
  getServerSnapshot: (scope?: string) => PermissionSnapshotState;
  /** Si `compiled` de `scope` concede `required`. Lee lo que ya está cargado; no fetchea. */
  grants: (scope: string, required: string) => boolean;
  /**
   * Asegura que `scope` esté cargado, fetcheando en segundo plano si hace falta, y **awaitea**
   * la resolución. Para el código que sí puede esperar (un loader de ruta, un botón que
   * deshabilita mientras confirma) — `can()`/`hasRole()` nunca esperan nada.
   */
  revalidate: (options?: { scope?: string }) => Promise<void>;
  /**
   * El consumidor vio un `AccessDeniedError` en su propio código. Marca `scope` (default: el
   * scope por defecto) `"stale"` y dispara una revalidación en segundo plano.
   */
  notifyAccessDenied: (scope?: string) => void;
  /** El estado de todos los scopes cargados, para persistir entre un render de servidor y el del cliente. */
  dehydrate: () => Record<string, PermissionSnapshotState>;
  /** Repuebla el store con lo que `dehydrate()` devolvió, **sin** disparar ningún fetch. */
  hydrate: (data: Record<string, PermissionSnapshotState>) => void;
  /** Da de baja la suscripción a la sesión y cualquier timer/listener pendiente. */
  dispose: () => void;
}

export function createPermissionStore(options: PermissionStoreOptions): PermissionStore {
  const scopeDefault = options.defaultScope ?? "";
  const scopes = new Map<string, ScopeEntry>();
  const listeners = new Set<() => void>();

  function notify(): void {
    for (const listener of listeners) listener();
  }

  function entryDe(scope: string): ScopeEntry {
    let entry = scopes.get(scope);
    if (!entry) {
      entry = {
        state: snapshotVacio(scope, "loading"),
        compiled: compilePermissions([]),
        enVuelo: null,
        ttlTimer: null,
      };
      scopes.set(scope, entry);
    }
    return entry;
  }

  function programarTtl(scope: string, entry: ScopeEntry): void {
    if (entry.ttlTimer !== null) clearTimeout(entry.ttlTimer);
    const { expiresAt } = entry.state;
    if (expiresAt === null) return;

    const demora = Math.max(0, expiresAt - Date.now());
    entry.ttlTimer = setTimeout(() => {
      marcarStale(scope);
      void fetchScope(scope);
    }, demora);
  }

  function marcarStale(scope: string): void {
    const entry = scopes.get(scope);
    if (!entry || entry.state.status !== "ready") return;
    entry.state = { ...entry.state, status: "stale" };
    notify();
  }

  async function fetchScope(scope: string): Promise<void> {
    const entry = entryDe(scope);
    if (entry.enVuelo) return entry.enVuelo;

    entry.enVuelo = ejecutarFetch(scope, entry).finally(() => {
      entry.enVuelo = null;
    });
    return entry.enVuelo;
  }

  async function ejecutarFetch(scope: string, entry: ScopeEntry): Promise<void> {
    try {
      const respuesta = await options.$fetch<MePermissionsResponse>(
        `/auth/rbac/me/permissions?scope=${encodeURIComponent(scope)}`,
      );
      entry.compiled = compilePermissions(respuesta.permissions);
      entry.state = {
        status: "ready",
        scope,
        roles: respuesta.roles,
        permissions: respuesta.permissions,
        version: respuesta.version,
        expiresAt: Date.parse(respuesta.expires_at),
      };
      programarTtl(scope, entry);
      notify();
    } catch {
      // Fail-closed en los datos (nunca se inventa un `allow`), pero **no** se pisa lo que ya
      // había: un blip de red no tiene por qué apagarle a alguien un botón que hace un segundo
      // veía bien — la próxima acción real la sigue decidiendo el servidor de todos modos. Si
      // nunca hubo una resolución exitosa, el snapshot vacío (`status: "loading"`) ya es
      // fail-closed por sí solo: `can()` da `false` contra un `CompiledPermissionSet` vacío.
      if (entry.state.status !== "ready") {
        entry.state = { ...entry.state, status: "stale" };
        notify();
      }
    }
  }

  function getSnapshot(scope: string = scopeDefault): PermissionSnapshotState {
    const entry = scopes.get(scope);
    if (!entry) {
      // Primer acceso a este scope: se agenda el fetch pero se responde ya, sin esperar —
      // `getSnapshot` nunca puede ser async.
      void fetchScope(scope);
      return snapshotVacio(scope, "loading");
    }
    return entry.state;
  }

  function grants(scope: string, required: string): boolean {
    const entry = scopes.get(scope);
    if (!entry) {
      void fetchScope(scope);
      return false;
    }
    return grantsPermission(entry.compiled, required);
  }

  async function revalidate(revalidateOptions: { scope?: string } = {}): Promise<void> {
    const scope = revalidateOptions.scope ?? scopeDefault;
    await fetchScope(scope);
  }

  function notifyAccessDenied(scope: string = scopeDefault): void {
    marcarStale(scope);
    void fetchScope(scope);
  }

  function limpiarTodo(): void {
    for (const entry of scopes.values()) {
      if (entry.ttlTimer !== null) clearTimeout(entry.ttlTimer);
    }
    scopes.clear();
  }

  const unsubscribeSession = options.session.subscribe(() => {
    const sesion = options.session.getSnapshot();
    if (sesion.status === "authenticated") {
      // Cubre sign-in, y cualquier re-autenticación (2FA, OAuth, passkey, impersonación): la
      // sesión nueva puede tener otros roles, así que se descarta todo lo cacheado.
      limpiarTodo();
      notify();
      void fetchScope(scopeDefault);
    } else if (sesion.status === "unauthenticated") {
      limpiarTodo();
      notify();
    }
  });

  // `visibilitychange`/`online` sólo si el runtime los tiene: en un worker, en SSR, o en
  // React Native no existen, y no son un requisito — son una mejora de UX en el navegador.
  let quitarVisibilidad: (() => void) | undefined;
  let quitarOnline: (() => void) | undefined;

  if (typeof document !== "undefined") {
    const alCambiarVisibilidad = () => {
      if (document.visibilityState !== "visible") return;
      for (const [scope, entry] of scopes) {
        if (entry.state.status !== "ready") void fetchScope(scope);
      }
    };
    document.addEventListener("visibilitychange", alCambiarVisibilidad);
    quitarVisibilidad = () =>
      document.removeEventListener("visibilitychange", alCambiarVisibilidad);
  }

  if (typeof window !== "undefined") {
    const alVolverOnline = () => {
      for (const [scope, entry] of scopes) {
        if (entry.state.status !== "ready") void fetchScope(scope);
      }
    };
    window.addEventListener("online", alVolverOnline);
    quitarOnline = () => window.removeEventListener("online", alVolverOnline);
  }

  function dehydrate(): Record<string, PermissionSnapshotState> {
    const resultado: Record<string, PermissionSnapshotState> = {};
    for (const [scope, entry] of scopes) {
      resultado[scope] = entry.state;
    }
    return resultado;
  }

  function hydrate(data: Record<string, PermissionSnapshotState>): void {
    for (const [scope, state] of Object.entries(data)) {
      const entry = entryDe(scope);
      entry.state = state;
      entry.compiled = compilePermissions(state.permissions);
      if (state.status === "ready") programarTtl(scope, entry);
    }
    notify();
  }

  function dispose(): void {
    unsubscribeSession();
    quitarVisibilidad?.();
    quitarOnline?.();
    limpiarTodo();
    listeners.clear();
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getSnapshot,
    getServerSnapshot: (scope = scopeDefault) => snapshotVacio(scope, "loading"),
    grants,
    revalidate,
    notifyAccessDenied,
    dehydrate,
    hydrate,
    dispose,
  };
}
