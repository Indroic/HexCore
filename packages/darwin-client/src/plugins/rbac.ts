/**
 * `rbac({ ac })`: el plugin de `client.rbac`, sobre `PermissionStore` (`../authz/store.ts`).
 *
 * **Todo lo que expone es optimista para la interfaz.** La autoridad de cualquier acción real
 * sigue siendo el servidor — este plugin nunca reemplaza el `require_permission(...)` de una
 * ruta protegida, sólo evita que la interfaz muestre o esconda de más mientras esa decisión
 * real todavía no se pidió.
 *
 * Uso::
 *
 *     const ac = defineAccessControl({
 *       resources: { invoice: ["read", "create", "update", "approve"], authz: ["manage"] },
 *       roles: ["viewer", "accountant", "admin"],
 *     });
 *     const client = createDarwinClient({ baseUrl, transport, plugins: [rbac({ ac })] });
 *
 *     client.rbac.can("invoice", "approve");                    // true|false, sin awaitear
 *     client.rbac.hasRole("accountant", { scope: "org:42" });
 *
 *     // React (o cualquiera con useSyncExternalStore/análogo):
 *     const snapshot = useSyncExternalStore(client.rbac.subscribe, client.rbac.snapshot);
 */

import type {
  AccessControl,
  ActionOf,
  Permission,
  ResourceOf,
  Schema,
} from "../authz/schema";
import type { PermissionSnapshotState, PermissionStore } from "../authz/store";
import { createPermissionStore } from "../authz/store";
import type { DarwinClientPlugin } from "../plugins";
import { definePlugin } from "../plugins";

export interface RbacOptions<S extends Schema, Roles extends string> {
  ac: AccessControl<S, Roles>;
  /** El scope por defecto de `can()`/`hasRole()`/`snapshot()` sin `{ scope }` explícito. Global si se omite. */
  scope?: string;
}

export interface RbacCheckOptions {
  /** Si se omite, usa el scope por defecto del plugin (`RbacOptions.scope`, o global). */
  scope?: string;
}

export interface RbacApi<S extends Schema, Roles extends string> {
  /**
   * Si el snapshot cargado de `options.scope` concede `resource.action`. **Síncrono**: nunca
   * dispara un fetch que haya que esperar — si el scope todavía no se cargó, lo pide en
   * segundo plano y responde `false` mientras tanto (nunca `true` sin datos: fail-closed).
   */
  can: <R extends ResourceOf<S>>(
    resource: R,
    action: ActionOf<S, R> | "*",
    options?: RbacCheckOptions,
  ) => boolean;
  /** Igual que `can`, con la clave ya armada (`"invoice.approve"`, `"invoice.*"`, `"*"`). */
  hasPermission: (permission: Permission<S>, options?: RbacCheckOptions) => boolean;
  /** Si alguno de los roles cargados es exactamente `role`. */
  hasRole: (role: Roles, options?: RbacCheckOptions) => boolean;
  /** El snapshot completo de un scope — para pintar una lista de roles, no sólo un booleano. */
  snapshot: (options?: RbacCheckOptions) => PermissionSnapshotState;
  /** Se dispara con cualquier cambio, en cualquier scope cargado. */
  subscribe: (listener: () => void) => () => void;
  /** Fetchea (o refetchea) `options.scope` y espera la resolución. */
  revalidate: (options?: RbacCheckOptions) => Promise<void>;
  /**
   * El consumidor vio un `AccessDeniedError` (`isAccessDenied()` en `core/errors.ts`) en su
   * propio código. Marca el scope "stale" y revalida en segundo plano.
   */
  notifyAccessDenied: (options?: RbacCheckOptions) => void;
  /** Para persistir entre un render de servidor y el del cliente. Nunca en `localStorage` por default. */
  dehydrate: () => Record<string, PermissionSnapshotState>;
  hydrate: (data: Record<string, PermissionSnapshotState>) => void;
}

export function rbac<S extends Schema, Roles extends string = never>(
  options: RbacOptions<S, Roles>,
): DarwinClientPlugin<"rbac", RbacApi<S, Roles>> {
  return definePlugin({
    id: "rbac",
    setup: (ctx): RbacApi<S, Roles> => {
      const scopeDefault = options.scope ?? "";
      const store: PermissionStore = createPermissionStore({
        $fetch: ctx.$fetch,
        session: ctx.session,
        defaultScope: scopeDefault,
      });

      function scopeDe(checkOptions?: RbacCheckOptions): string {
        return checkOptions?.scope ?? scopeDefault;
      }

      return {
        can: (resource, action, checkOptions) =>
          store.grants(scopeDe(checkOptions), `${resource}.${action}`),
        hasPermission: (permission, checkOptions) =>
          store.grants(scopeDe(checkOptions), permission),
        hasRole: (role, checkOptions) =>
          store.getSnapshot(scopeDe(checkOptions)).roles.includes(role),
        snapshot: (checkOptions) => store.getSnapshot(scopeDe(checkOptions)),
        subscribe: store.subscribe,
        revalidate: (checkOptions) => store.revalidate({ scope: scopeDe(checkOptions) }),
        notifyAccessDenied: (checkOptions) =>
          store.notifyAccessDenied(scopeDe(checkOptions)),
        dehydrate: store.dehydrate,
        hydrate: store.hydrate,
      };
    },
  });
}
