/**
 * `drbac({ ac })`: el plugin de `client.drbac`, sobre el AST de `../authz/conditions.ts` y el
 * `client.rbac` compilado en `../authz/matcher.ts`.
 *
 * Dos caminos, deliberadamente distintos:
 *
 * - **`evaluate()`**: síncrono y optimista, contra el resumen de `GET /auth/drbac/me/snapshot`
 *   —sólo reglas `client_evaluable`, ver el docstring de esa ruta del lado Python—. Nunca pega
 *   al servidor, nunca espera nada. Sirve para pintar la interfaz sin parpadeo: mostrar u
 *   ocultar un botón mientras se confirma la acción real.
 * - **`check()`/`checkMany()`**: asíncrono y **autoritativo**, contra `POST /auth/drbac/check`
 *   —el mismo `AuthorizationEngine.decide()` que corre en cada acción real, con RBAC, DRBAC y
 *   el scope retrocompatible ya combinados—. `check()` es azúcar sobre `checkMany()`: junta
 *   los pedidos que caen en el mismo microtask en un solo request, así que un componente que
 *   llama `check()` en un loop de render no dispara N requests.
 *
 * **Todo lo que expone `evaluate()` es optimista para la interfaz.** La autoridad de cualquier
 * acción real sigue siendo el servidor — este plugin nunca reemplaza `check()` antes de una
 * mutación real, sólo evita que la interfaz muestre o esconda de más mientras esa decisión
 * todavía no se pidió.
 *
 * Uso::
 *
 *     const ac = defineAccessControl({
 *       resources: { invoice: ["read", "approve"] },
 *     });
 *     const client = createDarwinClient({
 *       baseUrl, transport,
 *       plugins: [rbac({ ac }), drbac({ ac })],
 *     });
 *
 *     client.drbac.evaluate("invoice", "approve", { attributes: { owner_id: "u1" } });
 *     // "allow" | "deny" | "unknown" — sin awaitear nada
 *
 *     await client.drbac.check("invoice", "approve", { resourceId: "42" });
 *     // boolean — autoritativo, contra el servidor
 */

import type { Condition, ConditionEvaluator, EvaluationContext } from "../authz/conditions";
import { compileCondition } from "../authz/conditions";
import type { CompiledPermissionSet } from "../authz/matcher";
import { compilePermissions, grantsPermission } from "../authz/matcher";
import type { AccessControl, ActionOf, ResourceOf, Schema } from "../authz/schema";
import type { DarwinClientPlugin } from "../plugins";
import { definePlugin } from "../plugins";

//: TTL por defecto de una decisión cacheada de `check()`/`checkMany()` — corto a propósito:
//: es sólo para que una lista de N ítems que dispara N `check()` casi al mismo tiempo no
//: repita la misma pregunta al servidor, no para evitar revalidar de verdad.
const DEFAULT_DECISION_CACHE_TTL_MS = 5_000;

export interface DrbacOptions<S extends Schema, Roles extends string = never> {
  ac: AccessControl<S, Roles>;
  /** El scope por defecto de `evaluate()`/`check()` sin `{ scope }` explícito. Global si se omite. */
  scope?: string;
  /** TTL de la cache de decisiones de `check()`/`checkMany()`, en ms. */
  decisionCacheTtlMs?: number;
}

export interface DrbacCheckOptions {
  /** Si se omite, usa el scope por defecto del plugin (`DrbacOptions.scope`, o global). */
  scope?: string;
  /**
   * Atributos del recurso que el llamador ya tiene a mano — mismo criterio que
   * `ResourceRef.attributes` del lado Python: lo que falte, el servidor lo completa con su PIP.
   * Se llama `attributes` y no `resource` a propósito: en `checkMany()` cada ítem también
   * necesita un campo `resource` para el **nombre** del recurso (`"invoice"`), y los dos no
   * pueden compartir la clave.
   */
  attributes?: Record<string, unknown>;
  resourceId?: string;
  ownerId?: string;
}

export type DrbacEvaluation = "allow" | "deny" | "unknown";

export type DrbacSnapshotStatus = "loading" | "ready" | "stale";

export interface DrbacSnapshotState {
  status: DrbacSnapshotStatus;
  scope: string;
  version: number | null;
  expiresAt: number | null;
}

export interface DrbacApi<S extends Schema> {
  /**
   * `"allow"` / `"deny"` / `"unknown"` contra el resumen ya cargado. **Síncrono**: si el scope
   * todavía no se cargó, lo pide en segundo plano y responde `"unknown"` mientras tanto — nunca
   * `"allow"` sin datos.
   */
  evaluate: <R extends ResourceOf<S>>(
    resource: R,
    action: ActionOf<S, R> | "*",
    options?: DrbacCheckOptions,
  ) => DrbacEvaluation;
  /** Autoritativo: `POST /auth/drbac/check`, batcheado con cualquier otro `check()` del mismo microtask. */
  check: <R extends ResourceOf<S>>(
    resource: R,
    action: ActionOf<S, R> | "*",
    options?: DrbacCheckOptions,
  ) => Promise<boolean>;
  /** Como `check()`, para varios ítems en un solo request explícito. */
  checkMany: <R extends ResourceOf<S>>(
    items: ReadonlyArray<{ resource: R; action: ActionOf<S, R> | "*" } & DrbacCheckOptions>,
  ) => Promise<boolean[]>;
  /** El estado del resumen de un scope — para saber si `evaluate()` está respondiendo con datos o a ciegas. */
  snapshot: (options?: { scope?: string }) => DrbacSnapshotState;
  subscribe: (listener: () => void) => () => void;
  /** Fuerza a repedir el resumen de `options.scope` y espera la resolución. */
  revalidate: (options?: { scope?: string }) => Promise<void>;
}

interface CompiledSnapshotRule {
  effect: "allow" | "deny";
  actions: CompiledPermissionSet;
  resourceType: string;
  evaluator: ConditionEvaluator | null;
}

interface SnapshotResponse {
  scope: string;
  version: number;
  expires_at: string;
  rules: Array<{
    effect: "allow" | "deny";
    actions: string[];
    resource_type: string;
    condition: Condition | null;
  }>;
}

interface CheckResponseItem {
  action: string;
  resource_type: string;
  resource_id: string | null;
  allowed: boolean;
}

interface ScopeEntry {
  status: DrbacSnapshotStatus;
  rules: CompiledSnapshotRule[];
  version: number | null;
  expiresAt: number | null;
  ttlTimer: ReturnType<typeof setTimeout> | null;
  fetching: Promise<void> | null;
}

interface PendingCheckItem {
  action: string;
  resourceType: string;
  resourceId: string | null;
  ownerId: string | null;
  scope: string;
  attributes: Record<string, unknown>;
  resolvers: Array<(allowed: boolean) => void>;
}

interface DecisionCacheEntry {
  allowed: boolean;
  expiresAt: number;
  version: number | null;
}

function claveDeItem(item: {
  action: string;
  resourceType: string;
  resourceId: string | null;
  ownerId: string | null;
  scope: string;
  attributes: Record<string, unknown>;
}): string {
  return JSON.stringify([
    item.action,
    item.resourceType,
    item.resourceId,
    item.ownerId,
    item.scope,
    item.attributes,
  ]);
}

export function drbac<S extends Schema, Roles extends string = never>(
  options: DrbacOptions<S, Roles>,
): DarwinClientPlugin<"drbac", DrbacApi<S>> {
  return definePlugin({
    id: "drbac",
    // El registro se ordena por esto; DRBAC no llama a nada de `client.rbac` —los dos plugins
    // no se conocen, mismo criterio que del lado del servidor— pero declara la dependencia
    // igual, para que un consumidor que registre sólo `drbac()` se entere en el arranque.
    requires: ["rbac"],
    setup: (ctx): DrbacApi<S> => {
      const scopeDefault = options.scope ?? "";
      const ttl = options.decisionCacheTtlMs ?? DEFAULT_DECISION_CACHE_TTL_MS;

      const scopes = new Map<string, ScopeEntry>();
      const listeners = new Set<() => void>();
      const decisionCache = new Map<string, DecisionCacheEntry>();
      let pendientes: Map<string, PendingCheckItem> | null = null;

      function notify(): void {
        for (const listener of listeners) listener();
      }

      function scopeDe(checkOptions?: { scope?: string }): string {
        return checkOptions?.scope ?? scopeDefault;
      }

      function entryDe(scope: string): ScopeEntry {
        let entry = scopes.get(scope);
        if (!entry) {
          entry = {
            status: "loading",
            rules: [],
            version: null,
            expiresAt: null,
            ttlTimer: null,
            fetching: null,
          };
          scopes.set(scope, entry);
        }
        return entry;
      }

      function programarTtl(scope: string, entry: ScopeEntry): void {
        if (entry.ttlTimer !== null) clearTimeout(entry.ttlTimer);
        const { expiresAt } = entry;
        if (expiresAt === null) return;
        const demora = Math.max(0, expiresAt - Date.now());
        entry.ttlTimer = setTimeout(() => {
          const actual = scopes.get(scope);
          if (actual && actual.status === "ready") {
            actual.status = "stale";
            notify();
          }
          void fetchScope(scope);
        }, demora);
      }

      async function fetchScope(scope: string): Promise<void> {
        const entry = entryDe(scope);
        if (entry.fetching) return entry.fetching;
        entry.fetching = ejecutarFetch(scope, entry).finally(() => {
          entry.fetching = null;
        });
        return entry.fetching;
      }

      async function ejecutarFetch(scope: string, entry: ScopeEntry): Promise<void> {
        try {
          const respuesta = await ctx.$fetch<SnapshotResponse>(
            `/auth/drbac/me/snapshot?scope=${encodeURIComponent(scope)}`,
          );
          entry.rules = respuesta.rules.map(
            (regla): CompiledSnapshotRule => ({
              effect: regla.effect,
              actions: compilePermissions(regla.actions),
              resourceType: regla.resource_type,
              evaluator: regla.condition ? compileCondition(regla.condition) : null,
            }),
          );
          entry.version = respuesta.version;
          entry.expiresAt = Date.parse(respuesta.expires_at);
          entry.status = "ready";
          programarTtl(scope, entry);
          notify();
        } catch {
          // Fail-closed en los datos, no en el estado: un blip de red no debería apagar de golpe
          // un botón que hace un segundo se veía bien. Ver el mismo criterio en `authz/store.ts`.
          if (entry.status !== "ready") {
            entry.status = "stale";
            notify();
          }
        }
      }

      function contextoDe(
        scope: string,
        checkOptions?: DrbacCheckOptions,
      ): EvaluationContext {
        const sesion = ctx.session.getSnapshot();
        const subject: Record<string, unknown> =
          sesion.status === "authenticated"
            ? { id: sesion.me.actor_id, roles: sesion.me.roles ?? [] }
            : {};
        const resource: Record<string, unknown> = { ...checkOptions?.attributes };
        if (checkOptions?.resourceId !== undefined) resource.id = checkOptions.resourceId;
        if (checkOptions?.ownerId !== undefined) resource.owner_id = checkOptions.ownerId;
        resource.scope_path = scope;
        return { subject, resource, env: { now: new Date().toISOString() } };
      }

      function evaluate<R extends ResourceOf<S>>(
        resource: R,
        action: ActionOf<S, R> | "*",
        checkOptions?: DrbacCheckOptions,
      ): DrbacEvaluation {
        const scope = scopeDe(checkOptions);
        const entry = scopes.get(scope);
        if (!entry) {
          void fetchScope(scope);
          return "unknown";
        }

        const accionCompleta = `${resource}.${action}`;
        const candidatas = entry.rules.filter(
          (regla) =>
            regla.resourceType === resource &&
            grantsPermission(regla.actions, accionCompleta),
        );
        if (candidatas.length === 0) return "unknown";

        const contexto = contextoDe(scope, checkOptions);

        for (const regla of candidatas.filter((r) => r.effect === "deny")) {
          if ((regla.evaluator ? regla.evaluator(contexto) : true) === true) return "deny";
        }
        for (const regla of candidatas.filter((r) => r.effect === "allow")) {
          if ((regla.evaluator ? regla.evaluator(contexto) : true) === true) return "allow";
        }
        return "unknown";
      }

      function programarLote(): void {
        queueMicrotask(() => {
          const lote = pendientes;
          pendientes = null;
          if (lote) void resolverLote(lote);
        });
      }

      async function resolverLote(lote: Map<string, PendingCheckItem>): Promise<void> {
        const items = [...lote.values()];
        try {
          const respuesta = await ctx.$fetch<CheckResponseItem[]>("/auth/drbac/check", {
            method: "POST",
            body: {
              items: items.map((item) => ({
                action: item.action,
                resource_type: item.resourceType,
                resource_id: item.resourceId,
                owner_id: item.ownerId,
                scope_path: item.scope,
                attributes: item.attributes,
              })),
            },
          });
          items.forEach((item, i) => {
            const resultado = respuesta[i];
            const allowed = resultado ? resultado.allowed : false;
            decisionCache.set(claveDeItem(item), {
              allowed,
              expiresAt: Date.now() + ttl,
              version: scopes.get(item.scope)?.version ?? null,
            });
            for (const resolver of item.resolvers) resolver(allowed);
          });
        } catch {
          // Autoritativo y sin dato: fail-closed, nunca `true` por default.
          for (const item of items) {
            for (const resolver of item.resolvers) resolver(false);
          }
        }
      }

      function encolar(item: Omit<PendingCheckItem, "resolvers">): Promise<boolean> {
        const clave = claveDeItem(item);

        const enCache = decisionCache.get(clave);
        if (enCache && enCache.expiresAt > Date.now()) {
          const versionActual = scopes.get(item.scope)?.version ?? null;
          if (enCache.version === versionActual) {
            return Promise.resolve(enCache.allowed);
          }
        }

        if (pendientes === null) {
          pendientes = new Map();
          programarLote();
        }
        const existente = pendientes.get(clave);
        const entrada: PendingCheckItem = existente ?? { ...item, resolvers: [] };
        if (!existente) pendientes.set(clave, entrada);

        return new Promise((resolve) => {
          entrada.resolvers.push(resolve);
        });
      }

      function check<R extends ResourceOf<S>>(
        resource: R,
        action: ActionOf<S, R> | "*",
        checkOptions?: DrbacCheckOptions,
      ): Promise<boolean> {
        return encolar({
          action: `${resource}.${action}`,
          resourceType: resource,
          resourceId: checkOptions?.resourceId ?? null,
          ownerId: checkOptions?.ownerId ?? null,
          scope: scopeDe(checkOptions),
          attributes: checkOptions?.attributes ?? {},
        });
      }

      function checkMany<R extends ResourceOf<S>>(
        items: ReadonlyArray<
          { resource: R; action: ActionOf<S, R> | "*" } & DrbacCheckOptions
        >,
      ): Promise<boolean[]> {
        return Promise.all(
          items.map((item) =>
            check(item.resource, item.action, {
              ...(item.scope !== undefined ? { scope: item.scope } : {}),
              ...(item.attributes !== undefined ? { attributes: item.attributes } : {}),
              ...(item.resourceId !== undefined ? { resourceId: item.resourceId } : {}),
              ...(item.ownerId !== undefined ? { ownerId: item.ownerId } : {}),
            }),
          ),
        );
      }

      return {
        evaluate,
        check,
        checkMany,
        snapshot: (checkOptions) => {
          const scope = scopeDe(checkOptions);
          const entry = scopes.get(scope);
          return {
            status: entry?.status ?? "loading",
            scope,
            version: entry?.version ?? null,
            expiresAt: entry?.expiresAt ?? null,
          };
        },
        subscribe: (listener) => {
          listeners.add(listener);
          return () => {
            listeners.delete(listener);
          };
        },
        revalidate: (checkOptions) => fetchScope(scopeDe(checkOptions)),
      };
    },
  });
}
