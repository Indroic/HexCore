# Writing a Darwin plugin

Darwin ships six plugins, but the plugin system **is not for them**: it is the surface through
which your application injects its own identity logic without touching the core and without
forking.

A plugin can contribute HTTP routes, CQRS commands and queries, middleware, startup steps, tables
and — the most used of all — **hooks** that attach to the flows Darwin already has.

> All the code in this guide lives executed in
> [`tests/test_darwin_custom_plugin.py`](../../../tests/test_darwin_custom_plugin.py). If this
> guide lies, those tests go red. If you edit one, edit the other.

---

## The minimum a plugin is

A `name` and nothing else:

```python
from hexcore.darwin.domain.plugins import DarwinPlugin


class MyPlugin(DarwinPlugin):
    name = "my_plugin"
```

That already registers and validates. **Every extension point is a concrete method returning
empty**, so you declare only what you contribute. The reason is not convenience: with abstract
methods, every plugin would have to implement eight things to contribute one, and adding a new
extension point in a future version would break every existing plugin.

`name` is the identifier: it appears in registry errors, it is what other plugins put in
`requires`, and it is the package name `ensure_identity_schema_loaded(plugins=[...])` expects if
you contribute tables.

---

## The extension points

| Method | What it contributes | When Darwin calls it |
| :-- | :-- | :-- |
| `hooks()` | `HookBinding`s on identity actions | On every action, via `run_hooks` |
| `routers()` | `APIRouter`, or `(APIRouter, kwargs)` | At mount time, via `plugins.routers()` |
| `register_handlers(registry)` | Commands and queries into the `HandlerRegistry` | When CQRS is built |
| `exception_status_map()` | `{PluginException: status}` | `create_app` merges it |
| `middlewares()` | CQRS pipeline middleware | When the pipeline is built |
| `http_middlewares()` | `(class, kwargs)` for Starlette | When the app is built |
| `startup_steps()` | `StartupStep`s for `build_lifespan` | At startup |
| `tables()` | SQLAlchemy mixins, by name | **Never**: the consumer calls it |

And three class attributes:

| Attribute | Default | Purpose |
| :-- | :-- | :-- |
| `name` | — required | Identifier |
| `requires` | `()` | Other plugins that must be present **and run first** |
| `priority` | `100` | Tie-break between unrelated plugins. Lower runs first |
| `contributed_tables` | `()` | The names of `tables()`'s mixins, **without importing them** |

---

## Hooks: the extension point you will actually use

A hook attaches to an **action** in a **phase**:

```python
from hexcore.darwin.domain.plugins import DarwinPlugin, HookBinding


class BusinessHoursPlugin(DarwinPlugin):
    """Rejects sign-in outside a time window."""

    name = "business_hours"
    priority = 10

    def __init__(self, *, since: int = 8, until: int = 20) -> None:
        self._since = since
        self._until = until

    def hooks(self):
        return [
            HookBinding(
                action="user.sign_in",
                phase="before",
                handler=self._check_hours,
                priority=10,
            ),
        ]

    async def _check_hours(self, payload):
        hour = getattr(payload, "hour", None)
        if hour is not None and not (self._since <= hour < self._until):
            raise OutsideBusinessHoursError(
                f"Access is allowed from {self._since} to {self._until}."
            )
        return None
```

### The handler contract

It is `async (payload) -> payload | None`.

**Returning `None` means "I change nothing"**, and that is what most of them do — the hooks that
only observe. Returning a value **replaces** it for the next hook and for the handler. It chains
rather than accumulates: that is how a hook can refine what the previous one did.

Never mutate the argument. HexCore's messages are `frozen`, so mutating them does not type-check,
and that is deliberate.

### Wildcards and ordering

`action` accepts `fnmatch` wildcards: `"session.*"`, `"*"`. **Specific hooks run before wildcard
ones**, and the reason is practical: an audit hook with `"*"` wants to see the final payload, not
the one that arrived before the specific hooks adjusted it.

Within each group, `priority` decides: lower runs first.

---

## The trap: your exception has to be an `IdentityError`

This is the hardest thing to discover on your own, so it gets named explicitly.

`run_hooks` treats three cases differently:

- **`ShortCircuit`** propagates as-is. It is not an error: it is the mechanism a plugin uses to
  answer on its own.
- **`IdentityError`** propagates as-is, because it is a deliberate domain signal.
- **Any other exception** is wrapped in a `RuntimeError` naming the plugin, the phase and the
  action.

In other words: if your exception inherits from plain `Exception`, the framework treats it as **a
broken plugin**, and the consumer gets a 500 with a message about your plugin failing — not about
your business rule rejecting.

```python
from hexcore.darwin.domain.exceptions import IdentityError


class OutsideBusinessHoursError(IdentityError):   # IdentityError, not Exception
    """The plugin cuts sign-in outside the allowed window."""
```

And so it comes out with the right status instead of a 500, declare it:

```python
    def exception_status_map(self):
        return {OutsideBusinessHoursError: 403}
```

Exceptions live in the plugin and not in `domain/exceptions.py`: the core has no business knowing
your plugin's failure modes. `create_app` merges this map underneath identity's, which in turn
goes underneath the consumer's — so you can override it from your app.

That other exceptions get wrapped **is not hostility**: it is the plugin failing closed. Swallowing
them would let an authorization hook that blows up read as a hook that authorized.

---

## Short-circuiting: answering without running the handler

```python
from hexcore.darwin.domain.plugins import DarwinPlugin, HookBinding, ShortCircuit


class CachePlugin(DarwinPlugin):
    name = "cache"

    def __init__(self, response):
        self._response = response

    def hooks(self):
        return [
            HookBinding(
                action="user.sign_in",
                phase="before",
                handler=self._respond,
                priority=1,
            )
        ]

    async def _respond(self, payload):
        raise ShortCircuit(self._response)
```

In `before` it skips the handler **and the remaining `before` hooks**: the following hooks were
expecting a payload that is no longer going to be processed. In `after` it replaces the result.

It is the mechanism a 2FA plugin uses to cut a sign-in with "a second factor is required" instead
of letting it proceed.

---

## Action names

By default the action is **derived** from the message class's name:
`SignOutEverywhere` → `sign_out_everywhere`.

That is enough for what Darwin ships, but it binds the hook to the class name: renaming the
command would silently break every plugin's hooks. If you write your own commands that others will
attach to, name the action explicitly:

```python
from hexcore.darwin.domain.plugins import identity_action


@identity_action("user.sign_in")
class SignIn(Command):
    ...
```

The decorator returns **the same class, typed** — it does not degrade what it decorates.

---

## Ordering between plugins

`requires` declares dependencies and the registry validates them **at wiring time**, not on the
first request:

```python
class OnTop(DarwinPlugin):
    name = "on_top"
    requires = ("base",)
```

Four things are rejected right there, and each error names the culprit:

1. **Duplicate name.** Silently keeping one would make which plugin runs depend on import order.
2. **A `requires` that does not exist.** It would run anyway, without what it needs, and fail later
   in a place that does not point at the cause.
3. **A dependency cycle.** Here it is an error naming the cycle; in production it would be a
   `RecursionError` or an arbitrary order.
4. **A table conflict.** Two plugins contributing a mixin with the same name.

The order is **topological** by `requires`, with `(priority, registration order)` as a tie-break.
It is deterministic on purpose: if it depended on a set's hash, the same wiring would produce
different hook chains across runs.

**`requires` beats `priority`.** A plugin with `priority = 1` that requires one with
`priority = 900` still runs after it.

---

## Routes, commands and the rest

```python
    def routers(self):
        # Deferred import: keeps importing the plugin cheap, and does not require
        # [api] until somebody asks for the router.
        from my_package.router import build_router

        return [build_router()]

    def register_handlers(self, registry):
        from my_package.commands import MyCommand, MyCommandHandler

        registry.register_command_handler(
            MyCommand, registry.factory(MyCommandHandler)
        )
```

`register_handlers` receives the registry instead of returning a map because
`register_command_handler` distinguishes instances from factories, and returning a dict would force
you to reimplement that distinction.

---

## If your plugin stores things

Darwin does not impose a backend, and **your plugin should not either**. The core resolves storage
by **neutral-name contract**: each backend exposes the same names, and whoever collects them never
names a backend.

Expected structure:

```
my_plugin/
  __init__.py          the DarwinPlugin
  domain.py            the ports (Abstract*) and the entities
  orms/
    sqlalchemy/
      models.py        the mixins + PLUGIN_MODELS
      repository.py    the port's implementation
    beanie/
      repository.py    the documents + PLUGIN_DOCUMENTS
```

Two constants with **fixed names** are what make the schema reach Alembic and `init_beanie`:

- `PLUGIN_MODELS` in `orms/sqlalchemy/models.py`
- `PLUGIN_DOCUMENTS` in `orms/beanie/repository.py`

Without them your table exists in the database and is **missing from `Base.metadata`**, and the
next `alembic revision --autogenerate` emits an `op.drop_table` for it. With data in it. It is the
module's worst failure mode because it is the only one that raises nothing.

If your plugin implements only one backend, that is fine: whoever wires it with the other gets an
`ImportError` at startup saying which ones you implement. But **do not force it from
`pyproject`**: "one of two" cannot be expressed in packaging metadata, and declaring both would
install SQLAlchemy for someone who chose Mongo.

### `tables()` and `contributed_tables`

`tables()` returns **mixins**, not mapped classes. The consumer composes them with their `Base` in
their `models/` package, which is what makes `import_all_models` see them.

`contributed_tables` is that same list of names **without importing anything**. It is deliberate
duplication, and it exists for a concrete reason: the registry needs the names to detect the
conflict of two plugins with a same-named mixin, and calling `tables()` for that would import
SQLAlchemy — meaning a Mongo deployment could not even register your plugin.

```python
class MyPlugin(DarwinPlugin):
    name = "my_plugin"
    contributed_tables = ("MyMixin",)

    def tables(self):
        from my_package.orms.sqlalchemy.models import MyMixin

        return {"MyMixin": MyMixin}
```

Declaring it is optional — without a declaration the registry falls back to `tables()` — but it is
what makes your plugin usable on Mongo.

---

## Wiring it up

```python
from hexcore.darwin import IdentityConfig, configure_identity

configure_identity(
    IdentityConfig(),
    plugins=[BusinessHoursPlugin(since=8, until=20)],
)
```

`plugins=` accepts a list or tuple of instances, or an already-built `PluginRegistry`. An
uninstantiated class, a generator or a `set` are rejected with a `TypeError` that says why: a
generator is consumed once and a `set` has no order, and the plugins' order is what decides the
hooks' order.

And to mount their routes:

```python
from hexcore.darwin import build_identity_router, get_identity_container

plugins = get_identity_container().plugins

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    routers=[build_identity_router(), *plugins.routers()],
)
```

---

## Packaging it separately

If your plugin is its own distribution, follow Darwin's extras convention: make your extra **pull
in `hexcore[darwin]`**.

```toml
[project.optional-dependencies]
my-plugin = ["hexcore[darwin]", "whatever-you-need"]
```

Without that self-reference, `pip install 'my-package[my-plugin]'` installs a plugin with no core,
and the import breaks.

---

## Checklist

- [ ] `name` declared, unique and stable — it is public contract.
- [ ] The exceptions you raise on purpose inherit from `IdentityError`.
- [ ] They are in `exception_status_map()` with their status.
- [ ] Hooks are `async` and return `None` when they change nothing.
- [ ] You do not mutate the payload.
- [ ] Heavy imports are **deferred**, inside the method.
- [ ] If you contribute tables: `PLUGIN_MODELS` / `PLUGIN_DOCUMENTS`, and `contributed_tables`
      declared.
- [ ] If you have `requires`, you do not form a cycle.
- [ ] Your extra pulls in `hexcore[darwin]`.

---

## See also

- [The six bundled plugins](./bundled-plugins.md)
- [Storage, schema and Alembic](./storage.md)
- [`tests/test_darwin_custom_plugin.py`](../../../tests/test_darwin_custom_plugin.py) — all of
  this, executed
