/**
 * `compilePermissions()`/`grantsPermission()`: paridad con `CompiledPermissionSet` del lado
 * Python (`hexcore/darwin/plugins/rbac/matcher.py`).
 *
 * La semántica es la misma, letra por letra — incluido que un comodín **no** concede el nodo
 * pelado (`"users.*"` no concede `"users"`) — porque este módulo no inventa un lenguaje de
 * permisos propio del lado del cliente: sólo evalúa **de forma optimista**, para la interfaz,
 * el mismo conjunto que el servidor ya resolvió y mandó en `/auth/rbac/me/permissions`. La
 * autoridad para cualquier acción real sigue siendo el servidor.
 */

const SEPARATOR = ".";
const WILDCARD = "*";
const WILDCARD_SUFFIX = `${SEPARATOR}${WILDCARD}`;

export interface CompiledPermissionSet {
  readonly exact: ReadonlySet<string>;
  /** Los prefijos de comodín, **sin** el `.*` final: `"users.*"` queda `"users"`. */
  readonly wildcardPrefixes: ReadonlySet<string>;
  readonly hasGlobalWildcard: boolean;
}

/** Precompila un conjunto de permisos en bruto (la forma de `Permission.value` en Python). */
export function compilePermissions(permissions: Iterable<string>): CompiledPermissionSet {
  const exact = new Set<string>();
  const wildcardPrefixes = new Set<string>();
  let hasGlobalWildcard = false;

  for (const permiso of permissions) {
    if (permiso === WILDCARD) {
      hasGlobalWildcard = true;
    } else if (permiso.endsWith(WILDCARD_SUFFIX)) {
      wildcardPrefixes.add(permiso.slice(0, -WILDCARD_SUFFIX.length));
    } else {
      exact.add(permiso);
    }
  }

  return { exact, wildcardPrefixes, hasGlobalWildcard };
}

/**
 * Si `compiled` concede `required`.
 *
 * Uso::
 *
 *     const compilado = compilePermissions(["users.*", "invoice.read"]);
 *     grantsPermission(compilado, "users.invite");    // true, por el comodín
 *     grantsPermission(compilado, "invoice.read");    // true, exacto
 *     grantsPermission(compilado, "invoice.approve"); // false
 *     grantsPermission(compilado, "users");           // false: el comodín no concede el nodo pelado
 */
export function grantsPermission(
  compiled: CompiledPermissionSet,
  required: string,
): boolean {
  if (compiled.hasGlobalWildcard) return true;
  if (compiled.exact.has(required)) return true;
  if (compiled.wildcardPrefixes.size === 0) return false;

  // Los prefijos de `required`, de más específico a más general: "a.b.c" prueba "a.b" y
  // después "a", nunca "a.b.c" entero. A lo sumo profundidad(required) chequeos en un `Set`.
  const partes = required.split(SEPARATOR);
  for (let i = partes.length - 1; i > 0; i--) {
    if (compiled.wildcardPrefixes.has(partes.slice(0, i).join(SEPARATOR))) {
      return true;
    }
  }
  return false;
}
