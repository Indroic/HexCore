/**
 * `defineAccessControl()`: el esquema tipado de recursos/acciones/roles.
 *
 * Es sólo tipos + una identidad en runtime — **no valida nada contra el backend**. El
 * esquema que declarás acá y los permisos que `AuthorizationEngine` realmente resuelve del
 * lado del servidor pueden divergir con el tiempo (una acción nueva del servidor que todavía
 * no declaraste acá, por ejemplo), y eso es intencional: lo que este módulo te da es
 * autocompletado y un error de compilación ante un typo, no una fuente de verdad. La
 * autoridad sigue siendo el servidor, en cada `client.rbac.check()`/request real.
 *
 * Uso::
 *
 *     const ac = defineAccessControl({
 *       resources: { invoice: ["read", "create", "update", "approve"], authz: ["manage"] },
 *       roles: ["viewer", "accountant", "admin"],
 *     });
 *
 *     type P = Permission<typeof ac.resources>;
 *     // "invoice.read" | "invoice.create" | ... | "invoice.*" | "authz.manage" | "authz.*" | "*"
 */

/** Un recurso mapea a la lista de sus acciones. */
export type Schema = Readonly<Record<string, readonly string[]>>;

export type ResourceOf<S extends Schema> = keyof S & string;
export type ActionOf<S extends Schema, R extends ResourceOf<S>> = S[R][number];

/**
 * Todo permiso válido del esquema: `"recurso.accion"`, `"recurso.*"`, o el comodín total.
 *
 * La misma forma que `Permission`/`CompiledPermissionSet` entienden del lado Python — ver
 * `../authz/matcher.ts` — así que un valor de este tipo siempre es una clave válida para
 * `grantsPermission()`.
 */
export type Permission<S extends Schema> =
  | { [R in ResourceOf<S>]: `${R}.${ActionOf<S, R>}` | `${R}.*` }[ResourceOf<S>]
  | "*";

export interface AccessControlDef<S extends Schema, Roles extends string> {
  resources: S;
  /** Sólo para autocompletado de `hasRole()`. No se envía a ningún lado. */
  roles?: readonly Roles[];
}

export interface AccessControl<S extends Schema, Roles extends string> {
  readonly resources: S;
  readonly roles: readonly Roles[];
}

/**
 * Declara el esquema. `const S`/`const Roles` preservan los literales (`"invoice"`,
 * `"approve"`, `"admin"`...) en vez de ensancharlos a `string` — sin eso, `Permission<S>`
 * colapsaría a `string` y perdería el autocompletado y el chequeo en tiempo de compilación
 * que es todo el valor de esta función.
 */
export function defineAccessControl<
  const S extends Schema,
  const Roles extends string = never,
>(def: AccessControlDef<S, Roles>): AccessControl<S, Roles> {
  return { resources: def.resources, roles: def.roles ?? [] };
}
