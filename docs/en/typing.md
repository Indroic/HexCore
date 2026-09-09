# Typing

HexCore ships types: it includes `py.typed`, runs Pyright in `strict` mode over the whole package,
and publishes generated stubs for the facades. This document covers **what you need to know as a
consumer**; the full contract for contributors is in
[`docs/ARCHITECTURE_TYPING.md`](../ARCHITECTURE_TYPING.md) (Spanish).

---

## As a consumer

```sh
pip install hexcore
```

The package includes the PEP 561 marker (`py.typed`) and the `.pyi` files, so your type checker
sees the types without installing anything else. There is no separate `types-hexcore`.

⚠️ This was not always true, and the failure mode is subtle: `MANIFEST.in` governs the **sdist**,
not the wheel. For a while `pip install hexcore` delivered a package type checkers ignored,
because the marker was in the repository and not in the artifact. Today it is declared in
`[tool.setuptools.package-data]` **and there is a test that verifies it against the built wheel**.

### The facades and the stubs

The four facades (`hexcore.fastapi`, `hexcore.cqrs`, `hexcore.sql`, `hexcore.darwin`) resolve
their names at runtime with `__getattr__` (PEP 562). That is what lets `import hexcore.cqrs` work
with no extras installed.

The cost is that, unaided, **a checker would see `Any` for all 64 symbols of `hexcore.cqrs`**: a
`__getattr__` returning `t.Any` is exactly that. So each facade has a generated `.pyi` beside it,
with one `from … import X as X` per symbol.

Python uses the `.py` and the checker uses the `.pyi`, so **lazy loading is preserved and the
types are real**. You do not have to do anything to benefit from it.

The `.pyi` files are **generated** and carry a header saying so:

```sh
uv run python scripts/gen_stubs.py --write
```

A CI job (`stubs-drift`) reverts any hand edit.

---

## The house rule

There is one, and it explains most of the code's style:

> **A module is either a "leaf with an extra" or it is "core". There is no third option.**

- A **leaf with an extra** imports its third-party package at the top level, **without guards**.
  One definition, no branches, no `# type: ignore`. Either `sqlalchemy` is there or the module is
  not imported.
- A **core** module is importable with no extras, and defers resolution of the names that do need
  them.

The idiom this rule kills is `try: import X / except ImportError: X = None` followed by two
definitions of the same class. Pyright analyzes **both branches**, keeps the last one — the empty
one — and the type ends up `Any` even with the package installed. In other words: the pattern that
looked "defensive" was the one erasing the types.

The friendly error is preserved, but it becomes a **runtime** concern handled in **one place**:
`require_extra(module, para=...)` in
[`hexcore.capabilities`](./installation.md#importing-without-extras).

### `# type: ignore`

The policy is explicit: **no bare suppressions**. Each one is a `# pyright: ignore[rule]` with the
reason next to it, and `reportUnnecessaryTypeIgnoreComment` is set to `error`, so a suppression
that stopped being needed breaks the build.

The reason is that, without that rule, a suppression that no longer covers anything is
**indistinguishable** from one that still does, and the debt creeps back in the way it left.

---

## The gates

| Gate | What it does | When |
| :-- | :-- | :-- |
| `pyright hexcore` | `strict` mode, Python 3.12 | Always |
| `scripts/typing_ratchet.py` | Compares against `typing-baseline.json` | The real verdict |
| `stubs-drift` | Regenerates the `.pyi` files and fails if they changed | Always |
| `scripts/house_rules.py` | Verifies "leaf or core" | Always |
| `scripts/stub_quality.py` | `pyright --verifytypes`: what percentage of the public surface is genuinely typed, and does not let it drop | Always |
| `scripts/extra_smoke.py` | With **one** extra installed: that it suffices for its own and not for others' | CI matrix |
| `pytest -m typing` | The type tests in `tests/typing/` | Its own job |
| `pytest -m packaging` | Builds the wheel and verifies its contents | Its own job |

### Why a ratchet and not "zero errors"

The verdict is not Pyright's exit code but the ratchet: **existing debt is frozen and can only go
down**.

Turning every rule on from day one would put `master` red, and a gate that is red from the start
gets switched off — which is the one guaranteed way to never fix anything. Each rule is turned on
**after** the code it gates is clean.

The two still missing are named in `pyproject.toml`: `reportMissingTypeStubs = "error"` and
`enableTypeIgnoreComments = false`.

### The type tests

`tests/typing/` is **not executed**: those files are fed to Pyright. `norecursedirs` keeps them
out of pytest's collection, and `tests/test_typing_gate.py` is what invokes the checker.

A test asserting "this types as `Any`" has to be a file the checker reads, not one the interpreter
runs: at runtime, `Any` and the real type are indistinguishable.

---

## Writing typed code against HexCore

### Handlers

Inheriting from the abstract class is what gives the checker the result type:

```python
from hexcore.domain.cqrs import AbstractCommandHandler


class CreateTicketHandler(AbstractCommandHandler[CreateTicket, str]):
    async def handle(self, command: CreateTicket) -> str:
        ...
```

### Repositories

The two type parameters are the entity and the model:

```python
class TicketRepository(SqlAlchemyRepository[Ticket, TicketModel]):
    ...
```

### `ServerConfig` and the `t.Any` fields

`ServerConfig.cqrs` and `ServerConfig.darwin` are annotated `t.Any` on purpose: annotating them
properly would force `hexcore.config` to import those modules, and `hexcore.config` loads half the
framework. If you want the type in your own code, annotate it on the consumer side:

```python
from hexcore.darwin import IdentityConfig

identity: IdentityConfig = config.darwin
```

---

## Next

← Back to the **[index](./)**, or read the full contract in
[`ARCHITECTURE_TYPING.md`](../ARCHITECTURE_TYPING.md).
