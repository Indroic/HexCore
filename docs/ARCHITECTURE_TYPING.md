# Strict typing and stubs — HexCore

> Technical architecture document. The definitive `.pyi` strategy, the house rule for
> `TYPE_CHECKING`, and the CI/CD blueprint of the typing gate.
>
> Status: **design approved, Phase 0 implemented** (gate measuring, baseline frozen).
>
> **Out of scope: OpenAPI generation.** It is handled separately, as a FastAPI-specific
> utility.
>
> Some code blocks quote **verbatim output** from the repository's own scripts, which still
> print in Spanish. Those blocks are left untranslated on purpose: they are what you will
> actually see in your terminal.

---

## 1. The starting point, measured

Not estimated: measured with `pyright --outputjson` in `strict` mode over `--extra all`.

```
filesAnalyzed: 104    errorCount: 216    warningCount: 3    timeInSec: 12.67
```

**216 errors, 30 files in debt out of 104.** And the distribution is the finding that orders
the whole plan:

| file | errors | % of total |
| :-- | --: | --: |
| `hexcore/infrastructure/repositories/implementations.py` | 64 | 29 % |
| `hexcore/infrastructure/task_queues/procrastinate_adapter.py` | 34 | 15 % |
| `hexcore/infrastructure/task_queues/celery_adapter.py` | 28 | 12 % |
| `hexcore/infrastructure/repositories/utils.py` | 14 | 6 % |
| `hexcore/infrastructure/cqrs/postgres_bus.py` | 12 | 5 % |
| `hexcore/infrastructure/api/health.py` | 8 | 3 % |
| the rest (24 files) | 56 | 26 % |

**Three files hold 126 of the 216 errors (58 %),** and all three are exactly two of the seven
optional-import idioms: idiom B (fake class in `except ImportError`) in `implementations.py`,
and idiom E (`Celery = t.Any` inside `TYPE_CHECKING`) in both queue adapters.

By rule, the dominant errors are from the "unknown" family:

| rule | n |
| :-- | --: |
| `reportUnknownMemberType` | 64 |
| `reportUnknownArgumentType` | 36 |
| `reportUnknownVariableType` | 35 |
| `reportUnknownParameterType` | 9 |
| `reportUnusedFunction` | 8 |
| `reportInvalidTypeForm` | 8 |

That is the signature of `Any` propagating: these are not logic errors, they are types that
got lost and contaminated everything they touched. It is consistent with the diagnosis: idioms
B and E destroy types at the root and the rest is fallout.

**Tooling status before Phase 0:** `pyright>=1.1.405` was in the dev dependencies and **no
workflow ran it**. `.vscode/settings.json` had `"python.analysis.typeCheckingMode": "strict"`,
so the editor showed all 216 errors while CI reported green. 49 `# type: ignore` comments with
no rule code, meaning impossible to audit.

---

## 2. The seven idioms, and why they are one

Today **seven** different ways of guarding an optional import coexist. The house rule replaces
them with **one**.

| | Idiom | Where | Problem |
| :-- | :-- | :-- | :-- |
| **A** | `try: import / except ImportError:` + **a fake class in the except** | `repositories/base.py:1-13`, `types.py:1-9`, `repositories/utils.py:14-28` | Obscured declaration: Pyright resolves to the empty stub. `types.py` uses an inline `t.Generic[t.TypeVar("M")]`, which is invalid. `utils.py` has `bound=t.Union[..., t.Any]`, and that `t.Any` **degenerates the whole bound to `Any`**. |
| **B** | `try:` wrapping **the entire class body** | `implementations.py:43-57,139-141` and `:144-158,214-216` | **DX bug #1, 64 errors.** Pyright sees two declarations and resolves to `class SqlAlchemyRepository(t.Generic[T, M]): ...` — **no `save`, no `get_by_id`, no `model_cls`, no `query_cursor`**. |
| **C** | `except ImportError: pass` + `except NameError` around the class | `uow/__init__.py:1-11, 37-43, 122-123` | The `except NameError` is **dead code** (`from __future__ import annotations` means annotations are never evaluated). The real failure is an unguarded `isinstance(model, BaseModel)` at `uow/__init__.py:99` → `NameError` at runtime. |
| **D** ✅ | `if t.TYPE_CHECKING:` + a normal import | `redis_bus.py:18-20`, `postgres_bus.py:17-19`, `rabbitmq.py:17-19`, `uow/scopes.py:22-25`, … | **Correct.** Its only defect: two spellings coexist (`if t.TYPE_CHECKING:` vs `from typing import TYPE_CHECKING`). |
| **E** | `if t.TYPE_CHECKING: try: ... except ImportError: X = t.Any` | `celery_adapter.py:109-115`, `procrastinate_adapter.py:16-22` | **62 errors.** Useless: Pyright evaluates `TYPE_CHECKING` as `True` and analyzes **both branches**, so `Celery` ends up `type[Any]` and the signatures are `Any` **even with the extra installed**. |
| **F** | `_ensure_x()` with a friendly `ImportError` | **only** `cqrs/procrastinate.py:20-28` | It is the only one of the 8 adapters that says `pip install hexcore[procrastinate]`. The other 7 fail with upstream's raw `ModuleNotFoundError`. |
| **G** | `_x_available()` probes | `api/health.py:161-207` | `_sql_available` / `_mongo_available` / `_redis_configured`: three shapes and two naming conventions. No central registry, zero `HAS_*` constants. |

Plus an eighth: `implementations.py:103-122` does the imports **inside the method body** with a
string forward reference.

---

## 3. The house rule

### 3.1 One spelling

```python
from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
```

- **`import typing as t` + `if t.TYPE_CHECKING:`**, never `from typing import TYPE_CHECKING`.
  It is what most of the tree already uses; the rest gets unified.
- The block contains **only imports**. No `try`, no assignments, no `X = t.Any`.
- It goes immediately after the runtime imports.

### 3.2 Every module is either "leaf with an extra" or "core"

The missing distinction, and the one that makes the rule verifiable. It is declared in
`pyproject.toml`, so CI can check it:

```toml
[tool.hexcore.typing.extra_gated]
sql = [
  "hexcore.infrastructure.repositories.orms.sqlalchemy",
  "hexcore.infrastructure.repositories.orms.sqlalchemy.utils",
  "hexcore.infrastructure.repositories.orms.sqlalchemy.session",
  "hexcore.infrastructure.cqrs.cron_sql",
]
api = ["hexcore.infrastructure.api.app", "hexcore.infrastructure.api.routing", ...]
redis = ["hexcore.infrastructure.cache.cache_backends.redis", ...]
```

- **Leaf with an extra**: imports its extra at the top level, freely. Its docstring says so.
  It is only reached through a lazy facade.
- **Core**: **never** imports an extra at the top level. Only `if t.TYPE_CHECKING:` and lazy
  imports inside functions.

⚠️ Today that boundary is held up by **two undocumented 0-byte files**:
`cache/cache_backends/__init__.py` and `infrastructure/__init__.py`. If somebody adds a
re-export to the first one, `hexcore/__init__.py:20` (`from .infrastructure import cache`)
starts pulling in `redis`. The `no-extras-import` job turns that into a verified invariant.

### 3.3 The replacement for idiom B: **restructure, do not stub**

The hard case, and the highest-return one (64 errors). `SqlAlchemyRepository` has to keep its
completions **and** `hexcore.infrastructure.repositories.implementations` has to stay
importable without the extras, **and** `tests/test_optional_dependencies.py` has to keep
passing.

**A `.pyi` does not help here.** It would be a second copy of a ~100-line class maintained by
hand: the definition of maintenance debt.

**Solution: the guarded class moves to a leaf with an extra, and the core module re-exports it
lazily.**

Before (`implementations.py`, 64 errors):

```python
try:
    from .orms.sqlalchemy import BaseModel
    M = t.TypeVar("M", bound=BaseModel[t.Any])

    class SqlAlchemyRepository(BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]):
        @property
        def model_cls(self) -> t.Type[M]: ...
        async def save(self, entity: T) -> T: ...
        # ... ~100 lines
except ImportError:
    M = t.TypeVar("M")                                  # type: ignore
    class SqlAlchemyRepository(t.Generic[T, M]): ...    # type: ignore  ← Pyright resolves HERE
```

After:

```python
# hexcore/infrastructure/repositories/orms/sqlalchemy/repository.py
#   LEAF WITH EXTRA [sql]: imports sqlalchemy at the top level, unguarded.
#   One definition, no branches, no `# type: ignore`.
from .base import BaseSQLAlchemyRepository
from . import BaseModel

M = t.TypeVar("M", bound=BaseModel[t.Any])

class SqlAlchemyRepository(BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]):
    ...
```

```python
# hexcore/infrastructure/repositories/implementations.py
#   CORE: importable with no extras at all.

if t.TYPE_CHECKING:
    # The checker reads the REAL definitions. One declaration, nothing obscured.
    from .orms.sqlalchemy.repository import SqlAlchemyRepository as SqlAlchemyRepository
    from .orms.beanie.repository import BeanieRepository as BeanieRepository
else:
    # At runtime, PEP 562: resolved on first access, failing with a useful message rather
    # than a raw `ModuleNotFoundError` or an empty class.
    __getattr__ = lazy_attrs(__name__, {
        "SqlAlchemyRepository": (".orms.sqlalchemy.repository", "sql"),
        "BeanieRepository": (".orms.beanie.repository", "mongo"),
    })
```

Why the `else` and not just the `if`: if `__getattr__` is defined in both branches, Pyright
sees a module with a `__getattr__` and **stops reporting nonexistent attributes** on it —
typo detection is lost. With the `else`, the checker sees a closed, explicit namespace.

**The four contracts that are preserved, and how they are proven:**

| Contract | Proof |
| :-- | :-- |
| `import implementations` works without `sql`/`mongo` | `tests/test_optional_dependencies.py`, already exists |
| `SqlAlchemyRepository` has all its methods for Pyright | `tests/typing/` with `assert_type`; fails before, passes after |
| `hexcore.sql.SqlAlchemyRepository` still resolves | `tests/test_facades.py`, unchanged |
| The deprecated aliases keep resolving | `tests/test_deprecations.py`, unchanged |

### 3.4 The death of idiom E (62 errors)

```python
# BEFORE: useless. Pyright analyzes both branches, `Celery` ends up `type[Any]`, and
# `def __init__(self, app: "Celery")` is `Any` even with celery installed.
if t.TYPE_CHECKING:
    try:
        from celery import Celery
    except ImportError:
        Celery = t.Any

# AFTER: the type is real when the extra is there; the friendly error is a runtime concern.
if t.TYPE_CHECKING:
    from celery import Celery

class CeleryTaskEnqueuer:
    def __init__(self, app: "Celery") -> None:
        require_extra("celery", "CeleryTaskEnqueuer")   # ImportError carrying the pip install
        self._app = app
```

### 3.5 One friendly error, for all 8 extras

Generalizes the `_ensure_procrastinate()` that is today the only helpful one:

```python
# hexcore/_extras.py
_PACKAGE_BY_EXTRA = {
    "sql": "sqlalchemy", "api": "fastapi", "redis": "redis", "mongo": "beanie",
    "rabbitmq": "aio_pika", "procrastinate": "procrastinate", "celery": "celery",
    "darwin": "joserfc",
}


def require_extra(extra: str, consumer: str) -> None:
    """
    Checks that the extra is installed, or raises an `ImportError` with the remediation.

    All 8 adapters use this one. Before, 7 of 8 failed with upstream's raw
    `ModuleNotFoundError`, which says neither which extra to install nor why it was needed.
    """
    module = _PACKAGE_BY_EXTRA[extra]
    if importlib.util.find_spec(module) is None:
        raise ImportError(
            f"{consumer} needs the [{extra}] extra, which is not installed.\n\n"
            f"    pip install hexcore[{extra}]\n"
            f"    # or: uv add 'hexcore[{extra}]'\n"
        )
```

Plus a single capability registry (`hexcore/capabilities.py`) with `HAS_SQL`, `HAS_API`, …
constants replacing the three probes in `health.py`.

### 3.6 `# type: ignore` policy

Strict order, and **each step only after** the previous one is clean:

1. Narrow: `# type: ignore` → `# pyright: ignore[specificRule]` + the reason next to it.
2. `reportUnnecessaryTypeIgnoreComment = "error"` — CI tells you which ones are no longer
   needed.
3. `reportMissingTypeStubs = "error"` (needs the `typecheck` group with the stubs for
   croniter/asyncpg/celery/pika).
4. `enableTypeIgnoreComments = false`, **only** with a provably zero count. Turning it on with
   one outstanding silently un-suppresses whatever it was covering.

---

## 4. The `.pyi` strategy

### 4.1 Stubs for exactly three modules

A `.pyi` is a second copy that has to be kept in sync: it is debt. It is justified **only**
when no checker can evaluate the module's surface.

**Rule: inline by default. A `.pyi` only when the surface is computed at runtime.**

That leaves exactly three: `hexcore/cqrs.py` (54 entries), `hexcore/fastapi.py` (41) and
`hexcore/sql.py` (24). **119 symbols.**

### 4.2 The problem they solve

The three canonical facades — the ones the documentation advertises as "one obvious import per
task" — do this:

```python
_EXPORTS: dict[str, tuple[str, str]] = {
    "Base": ("hexcore.infrastructure.repositories.orms.sqlalchemy", "Base"),
    ...
}

__all__ = sorted(_EXPORTS)          # ← a RUNTIME expression: Pyright cannot evaluate it

def __getattr__(name: str) -> t.Any:  # ← EVERY export types as `Any`
    ...
```

**All 119 exports of the recommended public surface type as `Any`.**

And the irony: the **deprecated** surface does have shims that make it resolvable
(`domain/cqrs/__init__.py:100-104`, `buses.py:90-94`, `handlers.py:60-62`, `serializer.py:45-46`,
`middleware.py:57-58`, `uow/__init__.py:217-218`, `events.py:102-103`). The old surface types
correctly and the new one types `Any`.

### 4.3 Worked example: `hexcore/sql.py` + `hexcore/sql.pyi`

The source (unchanged, still lazy):

```python
# hexcore/sql.py
_EXPORTS: dict[str, tuple[str, str]] = {
    "Base": ("hexcore.infrastructure.repositories.orms.sqlalchemy", "Base"),
    "BaseModel": ("hexcore.infrastructure.repositories.orms.sqlalchemy", "BaseModel"),
    "init_engine": ("...sqlalchemy.session", "init_engine"),
    "SqlAlchemyUnitOfWork": ("hexcore.infrastructure.uow", "SqlAlchemyUnitOfWork"),
    ...
}

__all__ = sorted(_EXPORTS)

def __getattr__(name: str) -> t.Any:
    try:
        module_path, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'hexcore.sql' has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_path), attribute)
    globals()[name] = value          # cached: the second access does not come through here
    return value
```

The stub, **generated**:

```python
# hexcore/sql.pyi
# GENERADO por scripts/gen_stubs.py desde el `_EXPORTS` de hexcore/sql.py — NO EDITAR.
# Regenerá con: uv run python scripts/gen_stubs.py --write

from hexcore.infrastructure.repositories.orms.sqlalchemy import Base as Base
from hexcore.infrastructure.repositories.orms.sqlalchemy import BaseModel as BaseModel
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
    init_engine as init_engine,
)
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork as SqlAlchemyUnitOfWork

__all__ = [
    "Base",
    "BaseModel",
    "SqlAlchemyUnitOfWork",
    "init_engine",
]
```

**How Pyright resolves the pair, and why the stub does not break laziness.** Given `sql.py`
and `sql.pyi`, Pyright uses **the `.pyi` exclusively** and never looks at the `.py`. At
runtime, Python uses **the `.py` exclusively** and never looks at the `.pyi`. They do not
compete: the stub describes the static surface, the source implements the dynamic behavior.
`import hexcore.sql` still does not pull in sqlalchemy.

Non-obvious details:

- **The redundant `as X` is deliberate.** In a `.pyi`, an import without `as` is not
  considered re-exported (PEP 484). Without the `X as X`, the stub exports nothing.
- **A literal `__all__`**, which is what `sorted(_EXPORTS)` cannot be for a checker. Ordered
  identically to the runtime one, and there is a test that compares them.
- **No `def __getattr__`.** Declaring it would make Pyright accept any attribute, losing typo
  detection. The exception is `implementations.py`, which **does** need it for the deprecated
  aliases.

### 4.4 Generation and the drift gate

A generated stub that falls out of sync is worse than not having one: it promises symbols that
do not exist.

```python
# scripts/gen_stubs.py
"""
Genera los `.pyi` de las tres fachadas desde su `_EXPORTS`.

Trabaja sobre el **AST**, no importando el módulo: así no necesita ningún extra instalado
y funciona en el job más liviano del workflow.

    uv run python scripts/gen_stubs.py --write    # regenera
    uv run python scripts/gen_stubs.py --check    # falla si hay drift (lo que corre CI)
"""
```

A custom generator was chosen over the alternatives:

| Tool | Verdict |
| :-- | :-- |
| `stubgen` (mypy) | **No.** It imports or parses the module and emits `def __getattr__(name: str) -> Any`, which is exactly the problem. |
| `pyright --createstub` | **No.** Meant for untyped third-party libraries; it generates the full skeleton, not the re-export. |
| **Custom, from `_EXPORTS`** | ✅ `_EXPORTS` **is** the source of truth. 119 entries, a mechanical, deterministic mapping. |

`--check` fails with copy-pasteable remediation, in the house style:

```
::error::hexcore/sql.pyi está desincronizado con el _EXPORTS de hexcore/sql.py.
Regeneralo:

    uv run python scripts/gen_stubs.py --write
```

### 4.5 Type tests ✅ implemented

There were none: zero occurrences of `assert_type`, `reveal_type`, `pyright` or `py.typed`
under `tests/`. Now there is `tests/typing/` (checked by Pyright, never executed) plus
`tests/test_typing_gate.py` (12 tests, `typing` marker).

**The measurement that justifies this whole section**, with Pyright over the same file before
and after generating the stubs:

| symbol | before | after |
| :-- | :-- | :-- |
| `hexcore.sql.Base` | `Any` | `type[Base]` |
| `hexcore.sql.SqlAlchemyUnitOfWork` | `Any` | `type[SqlAlchemyUnitOfWork]` |
| `hexcore.sql.NAMING_CONVENTION` | `Any` | `dict[str, str]` |
| `hexcore.cqrs.Command` | `Any` | `type[Command]` |
| `hexcore.cqrs.HandlerRegistry` | `Any` | `type[HandlerRegistry]` |
| `hexcore.fastapi.create_app` | `Any` | the full signature, with all its kwargs |

The design:

```python
# tests/typing/test_facades_no_tipan_any.py
#   Never executed: it is fed to Pyright. `typing` marker, deselected by default.
from typing import assert_type

from hexcore.sql import Base, SqlAlchemyUnitOfWork


def test_las_fachadas_exponen_tipos_reales() -> None:
    # Fails BEFORE the stubs (everything was `Any`) and passes after. It is the executable
    # demonstration that problem #1 is dead.
    assert_type(Base, type[Base])
    assert_type(SqlAlchemyUnitOfWork, type[SqlAlchemyUnitOfWork])


def test_el_repositorio_generico_conserva_sus_parametros() -> None:
    from hexcore.sql import SqlAlchemyRepository

    class MiRepo(SqlAlchemyRepository[MiEntidad, MiModelo]):
        ...

    # It used to resolve to the empty stub from the `except ImportError`, making this an error.
    reveal_type(MiRepo().save)
```

Plus **negative tests**: an inverted `# pyright: ignore[...]` that pins the errors that
**must** keep appearing, so that turning a rule off cannot go unnoticed.

---

## 5. CI/CD blueprint

### 5.1 A new workflow, not jobs inside `pytest.yml`

A separate `typing.yml` was chosen:

- `pytest.yml` has its own strong invariant ("a single SKIPPED fails the job") that does not
  apply to typing. Mixing them makes a typing failure read as a test failure.
- The gate has to be callable via `workflow_call` from `publish-to-pypi.yml`.
- The job names of a matrix are dynamic and cannot be marked as required checks; an aggregator
  with a fixed name is needed, and that is a concept of the gate, not of the tests.

The triggers and the base-branch filter are copied from `pytest.yml`, including the
`release/**` and `feat/**` detail for stacked PR chains.

### 5.2 The jobs, and which phase each one enters in

| Job | What it does | Phase |
| :-- | :-- | :-- |
| **`typecheck`** | pyright strict + `::error file=,line=::` annotations + **ratchet** + `pytest -m typing` | **0 ✅** |
| **`typing-ok`** | Fixed-name aggregator for branch protection | **0 ✅** |
| **`stubs-drift`** | Regenerates and diffs. The fastest one: it neither imports hexcore nor needs extras | **T2 ✅** |
| `packaging` | Wheel with `py.typed` + the `.pyi` files; `twine check` | **0 ✅** (in `pytest.yml`) |
| `stub-quality` | `--verifytypes` with a completeness threshold that only goes up | 3 |
| `no-extras-import` | Installs **with no extras** and asserts every core module imports | 5 |
| `extras-matrix` | 9 legs: `none` + each extra + `all` | 5 |
| `house-rules` | The §3 rules, verified mechanically | 6 |

**The order matters and it is the lesson of this very repository.** Each job enters **after**
the code it gates is clean. The other way around, the first PR puts master red, and a gate
that starts red gets switched off — which is exactly how pyright ended up in the dev
dependencies and in no workflow.

### 5.3 The ratchet, and why not "zero errors"

Demanding zero with 216 errors of debt is a gate that gets turned off on the second PR.
Demanding "no worse than yesterday" can be turned on **today**.

`typing-baseline.json` stores a **per-file** budget plus the total. The rules:

1. A file that goes over its budget → **fails**.
2. A **new** file with any errors → **fails**. This is the clause that stops debt from growing:
   old debt is tolerated, new debt is not.
3. A file that improves → `::notice::` so somebody lowers the baseline. **Does not fail.**
4. **`--update` never runs in CI.** The baseline moves in a human PR, with a diff and a
   reviewer, so `git blame` keeps working for typing regressions.

Both regression modes were verified to fire:

```
$ python scripts/typing_ratchet.py errors --report pyright.json     # baseline tampered with
::error::hexcore/infrastructure/api/health.py: 8 error(es), el baseline permite 2.
exit=1

$ python scripts/typing_ratchet.py errors --report pyright.json     # file removed from baseline
::error::hexcore/infrastructure/api/health.py: 8 error(es) y no está en el baseline.
          Un archivo nuevo arranca en cero — la deuda vieja se tolera, la nueva no.
exit=1
```

Completeness uses the same mechanism with a **0.5 pp dead band**, so floating-point noise and
drift between pyright patch releases do not produce spurious "you improved" notices.

`PYRIGHT_VERSION` is pinned: a pyright upgrade that adds rules gets its own PR, whose diff is
`typing-baseline.json` plus the fixes — never a surprise inside somebody else's PR.

### 5.4 `--verifytypes` was blocked by packaging

`--verifytypes` resolves the package through PEP 561, so it needs `py.typed` in
`site-packages`. Without `[build-system]`, the package was not installed and it returned
`No py.typed file found` with completeness 0. **Phase 0 is what unblocked this job.**

And it installs the **wheel**, not an editable: setuptools editables use their own
`MetaPathFinder` that Pyright does not follow. Installing the wheel is deterministic and, as a
bonus, verifies that `py.typed` made it into the artifact that gets published.

### 5.5 The extras matrix closes the blind spot

Today CI runs a **single** `uv sync --extra all`, which hides exactly one class of bug: a
module from the `[redis]` extra that imports something from `[sql]` works under `all` and blows
up for whoever installed only `hexcore[redis]`.

9 legs: `none`, `api`, `redis`, `mongo`, `sql`, `rabbitmq`, `procrastinate`, `celery`, `all`.
It verifies **importability, not behavior**: running pytest with a single extra would trip 20
`importorskip` calls, and `pytest.yml`'s "zero SKIPPED" invariant is not to be touched.

⚠️ It is introduced with `continue-on-error: true` for **exactly one PR**. The matrix will find
real leaks the first time it runs — that is its purpose — and a matrix that starts red gets
deleted.

### 5.6 Hooking into `publish-to-pypi`

**Yes, publishing should depend on the gate**, and for a concrete rather than a general
reason: the only thing that makes typing reach the user is the wheel's `package-data`, which is
a **build-time** property. A green master proves nothing about the artifact if the artifact is
built by another workflow with another toolchain (`pip` + `python -m build`, against `uv`
everywhere else).

```yaml
jobs:
  gate:
    name: "Gate de tipado"
    uses: ./.github/workflows/typing.yml    # one definition, the same as in PRs
  build:
    needs: gate
```

Querying the Actions API (`gh api`) to check whether the gate's run on this SHA went green was
discarded: it needs `permissions: actions: read`, it races with the dispatch order of the two
workflows, and it fails confusingly while the run is still in progress.

**`bump_ver.yml` is left alone.** Putting the gate before the bump would mean a red master
blocks version bumps — which sounds right but is not: master is already red at that point, and
blocking the bump only adds a second failure to investigate.

**Branch protection: exactly two required checks** — `Python tests / test` and
`Gate de tipado`. Everything else arrives through `needs`.

---

## 6. Migration order

Each phase ends with master green.

| Phase | What | Risk |
| :-- | :-- | :-- |
| **T0 ✅** | `[tool.pyright]` strict with no new rules; ratchet; baseline of 216; `typing.yml` with `typecheck` + `typing-ok`; `ruff` moved to dev | **None.** The gate passes by definition on day one. |
| **T1 ✅** | `[build-system]`, `package-data`, `packages.find`, delete the root `__init__.py`, `tests/test_packaging.py` | Medium → **verified.** `uv sync` now installs the project; confirmed `hexcore.__file__` still points at the repo. |
| **T2 ✅** | The generated `.pyi` files for the 3 facades (`scripts/gen_stubs.py`), `tests/typing/`, `tests/test_typing_gate.py`, the `stubs-drift` job. **Verified with Pyright: the 126 exports went from `Any` to real types.** | Low, as expected. A `.pyi` cannot break runtime. |
| **T3** | `hexcore/capabilities.py` + `_extras.require_extra`; replace the 3 probes; declare `starlette` in `[api]` and `pymongo` in `[mongo]` | Low. |
| **T4** | The 7 idioms, **from smallest to largest radius**: E (62 err) → A in `types.py`/`base.py`/`utils.py` (~15) → C in `uow` → **B in `implementations.py` (64 err), last** | B is the risky one: it creates 2 files, deletes ~180 lines and changes what `hexcore.sql.SqlAlchemyRepository` resolves to. It goes last, against an already-clean baseline, with the four contracts of §3.3 verified. |
| **T5** | `test_optional_dependencies.py` derived from the matrix; the `no-extras-import` + `extras-matrix` jobs | Medium: it will find real leaks. `continue-on-error` for one PR. |
| **T6** | Narrow the `# type: ignore`s; turn on the 4 rules **in order** (§3.6); `house-rules` | Low if the order is respected. |
| **T7** | Unify the 3 deprecation mechanisms; PEP 702 `@deprecated`; `reportDeprecated = "error"` | Low. |
| **T8** | Hook the gate into `publish-to-pypi`; branch protection | The first release after this should be a deliberate `rc` tag. |

**What is NOT done in v6:** removing the pre-5.0 aliases. `REMOVED_IN` was already moved to
`7.0` in Phase 0. Whoever upgraded to 6.0.0 did so with the aliases present and working; taking
them away in a patch would be breaking the promise backwards. They are removed in a real 7.0.

---

## 7. Summary

Seven optional-import idioms collapse into **one**: `import typing as t` plus a single
`if t.TYPE_CHECKING:` containing nothing but imports, and every module classified — in a
CI-verified manifest — as **leaf with an extra** or **core**. The classes wrapped in `try` and
the fake stubs in `except ImportError` are not stubbed: they are **restructured**, moving the
guarded class into a leaf and re-exporting it with PEP 562 behind an `if not t.TYPE_CHECKING`,
so the checker reads the real definition and the namespace stays closed.

`.pyi` files exist for **exactly three modules** — the `_EXPORTS`-driven facades, whose surface
no checker can evaluate — and they are **generated from `_EXPORTS` with a drift gate**, so they
are not maintenance debt.

`require_extra()` gives all 8 adapters a friendly error; `hexcore.capabilities` gives one set
of `HAS_*` constants; and the four suppression rules are turned on in order, each after its
code is clean.

A reusable `typing.yml` runs pyright-strict behind a **per-file ratchet** seeded with the
**216 measured errors**, `--verifytypes` behind a completeness threshold that Phase 0 just
unblocked, a **9-leg matrix** that ends the `--extra all` blind spot, and a packaging job that
proves the wheel actually carries `py.typed` — with the publishing workflow invoking the same
gate, so a wheel without types cannot reach PyPI.
