# Autorización: `rbac`, `drbac` y el motor

Darwin siempre tuvo una forma barata de proteger una acción: `Principal.scopes` más
`require_scopes`, un match exacto de string contra lo que un `AbstractPrincipalResolver` puso en
el token. Eso sigue estando, y para una app chica puede ser todo lo que hace falta. Lo que no
puede responder es "¿este actor puede aprobar *esta* factura?" —una decisión que depende del
recurso, no sólo de lo que el actor trae encima— ni dejar que un admin asigne roles por una API
en vez de un deploy de código.

De eso trata esta página: `AuthorizationEngine` (el contrato del núcleo, siempre disponible), y
los dos plugins que lo usan — `rbac` (roles y permisos persistidos y asignables) y `drbac`
(reglas condicionales y bindings de rol contextuales, con vencimiento, sobre `rbac`).

```sh
pip install 'hexcore[darwin-rbac]'    # roles y permisos
pip install 'hexcore[darwin-drbac]'   # + políticas condicionales
```

---

## `AuthorizationEngine`: una pregunta, varios providers

El contrato del núcleo es agnóstico de quién contesta. Un `AuthorizationProvider` decide
`allow`, `deny` o `not_applicable` para un `AccessRequest`, y `AuthorizationEngine` combina todos
los providers registrados con **deny-overrides + default-deny**:

1. Cualquier `deny` gana, sin importar cuántos providers dijeron `allow`.
2. Sin ningún `deny`, gana el primer `allow`.
3. Si todos los providers contestaron `not_applicable` —cero providers registrados incluido— el
   motor deniega. Nada en este módulo lee el silencio como permiso.
4. Un provider que lanza se trata como `deny` y se loguea, nunca como que pasó.

`not_applicable` es lo que permite que varios providers convivan sin pisarse: un provider de
`drbac` que sólo conoce `invoice.*` no tiene nada que decir sobre `user.delete`, y contestar
`deny` ahí taparía bajo deny-overrides el `allow` de `rbac` para una acción que no tiene nada que
ver.

```python
from hexcore.darwin.infrastructure.api.authorization import require_permission
from hexcore.darwin.domain.authorization import ResourceRef

async def cargar_factura(request) -> ResourceRef:
    factura = await get_invoice(request.path_params["invoice_id"])
    return ResourceRef(type="invoice", id=str(factura.id), owner_id=factura.owner_id)

@router.post(
    "/invoices/{invoice_id}/approve",
    dependencies=[Depends(require_permission("invoice.approve", resource=cargar_factura))],
)
async def aprobar(invoice_id: UUID): ...
```

Para CQRS está `@authorize_command(action, resource_from=...)` (un decorador que estampa
metadata que lee `AuthorizationMiddleware`), y para código imperativo adentro de un handler está
`await authorize("invoice.update", ResourceRef(...))`, que usa el actor que ya está en
`AuthContext` y lanza `AccessDeniedError` — un 403 cuyo cuerpo lleva sólo `required`, nunca la
razón por la que perdió una política. Explicar la razón en la respuesta le regalaría a quien está
sondeando el sistema un mapa de lo que existe adentro.

Sin ningún plugin registrado, el único provider es `ScopeAuthorizationProvider` — el camino
retrocompatible sobre `Principal.scopes`, ahora con soporte de comodines (`Permission.grants`)
que el `require_scopes` a secas nunca tuvo.

---

## `rbac`: roles y permisos, persistidos y asignables

```python
from hexcore.darwin.plugins.rbac import RbacPlugin
from hexcore.darwin.domain.permissions import RoleRegistry

roles = (RoleRegistry()
    .register_role("viewer", permissions={"invoice.read"})
    .register_role("accountant", permissions={"invoice.create", "invoice.approve"}, inherits={"viewer"})
    .register_role("admin", permissions={"invoice.*", "authz.*"}, inherits={"accountant"}))

rbac = RbacPlugin(registry=roles, org_role_mapping={"owner": "admin", "member": "viewer"})
configure_identity(
    IdentityConfig(),
    plugins=[rbac],
    principals=rbac.principal_resolver(),   # puebla Principal.roles en el sign-in y en el refresh
)
```

Los roles de `RoleRegistry` ("roles de código") siguen siendo la fuente de verdad de qué
**significa** un rol. `RbacPlugin.startup_steps()` los siembra en la tabla como `is_system=True`
—no editables por `PATCH`/`DELETE /roles/{id}`, porque un rol de código puede estar hardcodeado
en las rutas y los hooks de tu app, y un panel que deja borrarlo no puede ver eso. Lo que la
tabla agrega encima son roles **por tenant**, asignaciones **con vencimiento**, y un punto de
decisión.

### Scope, la convención de tenancy

Toda tabla lleva `scope_key: str`, y **nunca es `NULL`** — `""` es global. Un `UNIQUE` con `NULL`
no rechaza duplicados ni en SQL ni en Mongo (cada `NULL` es distinto de sí mismo), así que `""`
es lo que hace que `UNIQUE(scope_key, name)` signifique algo.

`RbacAuthorizationProvider` **no** recorre la jerarquía de scope: `"org:1/proj:2"` es una clave
exacta, no un prefijo que también mire `"org:1"`. Si necesitás que "un rol asignado a nivel org
también aplique a sus proyectos", esa búsqueda jerárquica es lo que dan los bindings de rol
contextuales de `drbac` — ver más abajo.

### Anti-escalada

Nadie —ni siquiera un actor con `authz.manage`— puede otorgar un rol o un permiso que exceda lo
que él mismo tiene efectivamente en ese scope, ya sea editando los permisos directos de un rol,
cambiando de quién hereda, o asignándoselo a otra persona:

```python
await servicio.set_role_permissions(
    actor_id=admin.id, role_id=rol.id, permission_keys=["invoice.approve"],
)
# lanza EscalationError si `admin` no tiene ya "invoice.approve" (o un comodín que lo cubra)
# en el scope de ese rol.
```

> **El problema del bootstrap, y su único bypass sancionado.** En un despliegue nuevo nadie
> tiene todavía ningún permiso efectivo, así que el primerísimo `assign_role` no tiene contra
> qué comparar. `assign_role(actor_id=None, ...)` es la salida de emergencia — un otorgamiento
> del sistema, que saltea la anti-escalada por completo— pensada para un script de seed o la CLI
> sin `--actor-id`. No es un agujero silencioso: la asignación queda auditada con
> `granted_by=None`, que en cualquier auditoría se lee como "esto lo otorgó el sistema, no una
> persona".

### Invalidación sin esperar ningún TTL

Cada mutación que puede cambiar lo que alguien puede hacer sube `darwin_authz_version` para ese
scope en la misma operación. La clave del cache de la matriz de permisos lleva la versión
adentro (`darwin:rbac:{scope}:v{version}:{user_id}`), así que revocar un rol no necesita borrar
ninguna entrada de cache — la clave vieja simplemente nunca se vuelve a pedir, y el LRU o el
`ICache` compartido la desaloja solo, en su propio horario. El siguiente request que resuelva los
permisos de ese usuario en ese scope recibe la respuesta actual.

### `embed_in_token`

`RbacPrincipalResolver` decide qué tan gordo sale el token:

- `"roles"` (default): sólo los **nombres** de rol viajan en `Principal.roles`. Los permisos se
  resuelven del lado del servidor, cacheados, en cada `AuthorizationEngine.decide()`.
- `"roles_and_permissions"`: los permisos efectivos también van a `Principal.scopes`, así que el
  `ScopeAuthorizationProvider` retrocompatible también los ve — útil mientras migrás un
  despliegue que todavía usa `require_scopes` en algunas rutas.

### CLI

```sh
hexcore darwin rbac sync            # vuelve a sembrar el RoleRegistry en la tabla
hexcore darwin rbac list --scope org:1
hexcore darwin rbac assign --user <id> --role <id> --scope org:1
```

`rbac_cli` es un `typer.Typer` suelto que montás vos mismo — el núcleo no puede importar un
plugin por nombre, así que nada lo cablea automáticamente en el árbol de comandos de `hexcore`.

---

## `drbac`: condiciones, sobre `rbac`

```python
from hexcore.darwin.plugins.drbac import DrbacPlugin

drbac = DrbacPlugin(
    resolvers={"invoice": InvoiceAttributes(uow_scope)},   # el PIP, ver más abajo
    role_permissions=rbac.service().permission_keys_for_role_name,
)
configure_identity(IdentityConfig(), plugins=[rbac, drbac], principals=rbac.principal_resolver())
```

`DrbacPlugin.requires = ("rbac",)` — DRBAC extiende RBAC, no lo reemplaza. Y sin embargo
**ningún módulo bajo `hexcore.darwin.plugins.drbac` importa nada de
`hexcore.darwin.plugins.rbac`**. `requires` sólo ordena el registro de plugins; se valida por
nombre, nunca por import. Un `RoleBinding.role_name` es un string suelto, no una clave foránea a
las tablas de `rbac`, y el único puente real —expandir un nombre de rol contextual a los
permisos que otorga— es el callable `role_permissions` de arriba, cableado por *vos*, el mismo
patrón que ya usa la integración opcional de `rbac` con `organization`. Que los dos plugins
genuinamente no se conozcan es lo que permite instalar uno sin que el otro se arrastre nunca.

### Políticas y reglas

Una `Policy` vive en un scope, tiene una prioridad, y lleva una lista ordenada de `Rule`s:

```python
await drbac_servicio.create_policy(
    scope_key="org:42",
    name="no-self-approval",
    rules=[{
        "effect": "deny",
        "actions": ["invoice.approve"],
        "resource_type": "invoice",
        "condition": Eq(Var("resource.owner_id"), Var("subject.id")),
    }],
)
```

`actions` usa la misma forma `"recurso.accion"` / `"recurso.*"` / `"*"` de comodines que
`Permission.grants` en el resto de Darwin.

### El lenguaje de condiciones

Un AST declarativo, a propósito en vez de un `eval()` sobre un string o un DSL propio: es dato,
así que serializa a JSON tal cual, se valida solo al construirse, y comparte una sola forma con
el cliente TypeScript. No hay ningún camino de una condición guardada a ejecución de código
arbitrario.

| Nodo | Significa |
| :-- | :-- |
| `Eq`, `Ne`, `Gt`, `Gte`, `Lt`, `Lte` | Comparaciones |
| `In`, `Contains` | Pertenencia, cada uno el espejo del otro |
| `StartsWith` | Prefijo de string |
| `WithinScope` | `left` está en o debajo del ancestro `right`, **por segmento de path** |
| `TimeBetween` | `start <= value <= end` |
| `And`, `Or`, `Not` | Combinadores, de tres valores (ver abajo) |
| `Var` | Lee `subject.*` / `resource.*` / `env.*` — nada más, validado al construir el `Var` |
| `Const` | Un literal |
| `Predicate` | Una salida de emergencia con nombre para lo que el AST no puede expresar (ver abajo) |

**Tri-estado, lógica de Kleene — las mismas reglas que el `NULL` de SQL.** `true`/`false`
deciden; un `Var` que no resuelve, un predicado que nadie registró, o tipos no comparables dan
`None` ("indeterminado") — nunca se adivina hacia `allow`. `And`/`Or` lo propagan exactamente
igual que SQL: un `false` en un `And`, o un `true` en un `Or`, domina sobre cualquier
indeterminado de sus hermanos.

> **`WithinScope` compara por segmento, nunca por prefijo de string crudo.** `"org:4"` no es
> ancestro de `"org:42/..."` sólo porque el string sea prefijo de él — ese bug específico es
> exactamente la escalada cross-tenant de la tabla de riesgos de más abajo.

Un `Predicate("business_hours")` resuelve, por nombre, a una función registrada explícitamente
en este proceso con `@registry.predicate("business_hours")`. No encontrar uno registrado es
indeterminado, nunca un error que tumbe el request. Los predicados sólo corren en el servidor —
una regla cuya condición use uno nunca es `client_evaluable`, forzada a `False` sin importar lo
que se haya declarado al guardar la política, porque el navegador no tiene forma de saber qué
hace un predicado sin correr el mismo Python.

Los límites de tamaño (`ConditionLimits`, 256 nodos / 16 niveles por default) se aplican **al
guardar** una política, no sólo al evaluarla — una política se escribe una vez y se evalúa en
cada request que matchea, así que rechazar un árbol enorme recién al evaluar ya habría pagado el
costo de haberlo persistido e indexado.

### Jerarquía de scope y bindings de rol contextuales

Al revés que el `scope_key` plano de `rbac`, `drbac` resuelve toda la cadena de ancestros del
`scope_path` de un recurso: una política en `"org:1"` también aplica a `"org:1/proj:2"`. Un
`RoleBinding` agrega encima de esa jerarquía un rol contextual, con vencimiento opcional — un rol
que sólo existe en un scope dado y sus descendientes, opcionalmente condicionado por su propia
condición, y que nunca toca `Principal.roles` en el JWT.

### Cómo se toma una decisión

1. `scope_chain(resource.scope_path)` — todos los ancestros, del más general al más específico.
2. Por capa, las reglas ya compiladas y cacheadas de sus políticas habilitadas
   (`PolicySetCache`, versionado por un contador de authz-version **propio** de `drbac` —
   tabla propia, no la de `rbac`, otra vez porque los dos plugins no comparten nada).
3. Filtro barato: sólo las reglas cuyo `resource_type` y `actions` coinciden con el pedido.
4. El contexto de evaluación: `subject` (los roles del token más cualquier `RoleBinding`
   vigente), `resource` (completado por el PIP, ver abajo), `env` (`now`, más lo que traiga
   `AccessRequest.environment`).
5. **Un deny que resuelve `true` gana; si no, un allow que resuelve `true` gana; si no, si algo
   fue indeterminado, deniega (fail-closed); si no, `not_applicable`** — DRBAC no tiene nada que
   decir, y el combinador sigue con `rbac` o el provider de scope.

### El PIP: completa `resource.*` bajo demanda

`ResourceRef.attributes` es sólo lo que el llamador ya tenía a mano — cargar el recurso entero
"por las dudas" en cada endpoint protegido sería trabajo desperdiciado la mayoría de las veces,
porque la mayoría de las condiciones no lo necesitan. Un `ResourceAttributeResolver` registrado
por `resource.type` completa lo que falte cuando una condición efectivamente lo referencia:

```python
class InvoiceAttributes(ResourceAttributeResolver):
    resource_type = "invoice"

    async def resolve(self, resource: ResourceRef) -> Mapping[str, Any]:
        async with uow_scope() as uow:
            factura = await uow.invoices.get(UUID(resource.id))
        return {"status": factura.status, "amount": factura.amount}
```

Se memoiza por lote (`POST /auth/drbac/check` evalúa hasta 50 ítems), y un fallo del resolver
nunca propaga — se loguea, y la condición que necesitaba ese atributo queda indeterminada en vez
de tumbar toda la decisión.

### Del lado del cliente: `/me/snapshot` y `POST /check`

`GET /auth/drbac/me/snapshot` devuelve sólo las reglas `client_evaluable` de un scope —sin
bindings, sin políticas deshabilitadas, nada con un `Predicate` adentro— para el `evaluate()`
del cliente TypeScript, que es puramente optimista y nunca la autoridad. `POST /auth/drbac/check`
es la posta: el mismo `AuthorizationEngine.decide()` que corre en cada ruta protegida,
batcheado y deduplicado. Ver la
[documentación de plugins de `@hexcore-js/darwin-client`](../../../darwin-client/es/plugins.md#drbac)
para el lado del cliente.

---

## Modelo de amenazas

| Riesgo | Vector | Mitigación |
| :-- | :-- | :-- |
| Escalada vía asignación de rol | Un admin de tenant se asigna un rol con `*` | Anti-escalada: sólo lo que el actor ya tiene efectivamente en ese scope; los roles `is_system` son inmutables por API |
| Escalada vía impersonación | Impersonar a un admin | `AuthorizationEngine` siempre evalúa al **actor**, nunca al subject — la misma regla que ya aplica `AuthContext.has_scope` |
| Escalada cross-tenant | Un binding en `"org:4"` aplicado a `"org:42"` | `WithinScope` y la resolución de la cadena de scope comparan **por segmento de path**, nunca por prefijo de string crudo |
| Política maliciosa o enorme | Un árbol de condición gigante o muy anidado | Sin `eval`, sin regex en el AST; límites de tamaño aplicados al guardar y de nuevo defensivamente al compilar |
| Staleness tras revocar | Roles embebidos en un JWT viven hasta `access_ttl` | Los contadores `pv`/versión hacen que el cache falle inmediatamente; `revoke_all_for` existe para un corte inmediato |
| Desync cliente-servidor | La UI muestra un botón que el servidor rechazaría | La UI siempre es optimista; un `AccessDeniedError` atrapado dispara revalidación; `evaluate()` da `"unknown"` en vez de adivinar `"allow"` |
| Fuga de información | El campo `reason` de un 403 | `Decision.reason` es sólo interno — el cuerpo lleva `required`, nunca por qué perdió una política. `explain` sólo existe detrás de `POST /simulate`, a su vez protegido por `authz.debug` |
| Predicado como camino de ejecución | Una condición que intenta correr código arbitrario | No hay `eval`; un `Predicate` sólo resuelve a una función registrada explícitamente en este proceso por nombre, y sólo corre del lado del servidor |

---

## Ver también

- [Plugins incluidos](./plugins-incluidos.md) — la lista completa, `rbac` y `drbac` incluidos
- [Plugins de `@hexcore-js/darwin-client`](../../../darwin-client/es/plugins.md) — `rbac()` y `drbac()` del lado del cliente
- [Escribir un plugin propio](./plugins-propios.md)
