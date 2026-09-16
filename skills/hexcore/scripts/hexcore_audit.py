#!/usr/bin/env python3
"""
Audit a project that consumes HexCore.

Two groups of findings.

**Removed and deprecated API.** Names that were deleted in 7.0 or deprecated in 9.0. These
are mechanical renames, and the point of catching them is that a project upgrading from 2.x
has no other way to find them all.

**Silent failure modes.** This is the group that earns the script. HexCore's documentation
marks about twenty traps with a warning, and they share a property: *almost none of them
raise*. A missing line in `env.py` produces a migration that is generated cleanly and drops a
table with data in it when applied. `RetryMiddleware` on top of the queue's retry runs a
handler twelve times instead of six. The UoW publishes nothing at all and logs nothing. A
reviewer who has not memorised the list cannot see any of it, because there is nothing to
see.

Everything here is AST-based, not grep: `TransactionMiddleware` in a comment is not a
finding, and `metadata` as a local variable is not a shadowed `Base.metadata`.

Usage
-----
    python hexcore_audit.py                     # audit the current directory
    python hexcore_audit.py src/ --fail-on high
    python hexcore_audit.py --json
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path


def parse_quietly(source: str, filename: str) -> ast.Module:
    """
    Parsing someone else's code raises their SyntaxWarnings -- an invalid escape in a
    docstring, say. Those belong to their build, not to this audit, and left alone they end
    up interleaved with `--json` output the moment anyone redirects stderr.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(source, filename=filename)


SEVERITIES = ("critical", "high", "medium", "low")

#: Deleted in 7.0 -> what replaces it. Deprecated since 5.0, removed two majors later; the
#: replacement is a rename, not a behavior change.
REMOVED_IN_7: dict[str, str] = {
    "SQLAlchemyCommonImplementationsRepo": "SqlAlchemyRepository",
    "BeanieODMCommonImplementationsRepo": "BeanieRepository",
    "NoSqlUnitOfWork": "BeanieUnitOfWork",
    "IEventDispatcher": "AbstractEventBus",
    "InMemoryEventDispatcher": "hexcore.cqrs.InMemoryEventBus",
    "ICommandBus": "AbstractCommandBus",
    "IQueryBus": "AbstractQueryBus",
    "IEventBus": "AbstractEventBus",
    "ICommandHandler": "AbstractCommandHandler",
    "IQueryHandler": "AbstractQueryHandler",
    "IMiddleware": "AbstractMiddleware",
    "ISerializer": "AbstractSerializer",
    "MiddlewareConfig": "nothing -- it was dead code, never read, removed in 3.0",
    "reset_sqlalchemy_engine": "dispose_engine",
}

#: Deprecated in 9.0, removed in 10.0. Read from the installed package when
#: `hexcore_surface.py` sits next to this file; this is the fallback.
DEPRECATED_IN_9: dict[str, str] = {
    "EventBus": "hexcore.domain.cqrs.buses.AbstractEventBus",
    "PermissionsRegistry": "hexcore.darwin.RoleRegistry",
    "TokenClaims": "hexcore.darwin.AccessTokenClaims",
}

#: The base CRUD a generic repository already implements. Overriding one is almost always a
#: misunderstanding of what the base class gives you.
BASE_REPOSITORY_METHODS = {
    "get_by_id",
    "get_active_by_id",
    "list_all",
    "query_all",
    "query_cursor",
    "save",
    "delete",
}

#: `BaseEntity` already carries these. Redeclaring them in a subclass shadows the base field.
BASE_ENTITY_FIELDS = {"id", "created_at", "updated_at", "is_active"}

#: Mixins that compose a *framework* table. A model that mixes one of these in must not also
#: inherit `BaseModel[T]`: the UoW would ask it for a domain entity it does not have, during
#: `commit()`.
FRAMEWORK_MIXINS = {
    "CronJobModelMixin",
    "EventStoreMixin",
    "SnapshotMixin",
    "ProjectionCheckpointMixin",
    "UserMixin",
    "SessionMixin",
    "AuditLogMixin",
}

GENERIC_REPOSITORIES = {
    "SqlAlchemyRepository",
    "BeanieRepository",
    "BaseSQLAlchemyRepository",
    "BaseBeanieRepository",
}


@dataclass
class Finding:
    severity: str
    rule: str
    path: str
    line: int
    message: str
    fix: str
    doc: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "fix": self.fix,
            "doc": self.doc,
        }


# ---- helpers -----------------------------------------------------------------


def call_name(node: ast.AST) -> str:
    """The callable's bare name: `cqrs.TransactionMiddleware(...)` -> TransactionMiddleware."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return call_name(node.value)
    return ""


def base_names(cls: ast.ClassDef) -> set[str]:
    """Every base's bare name, `Repo[Entity, Model]` included."""
    return {call_name(base) for base in cls.bases} - {""}


def keyword_of(node: ast.Call, name: str) -> ast.keyword | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword
    return None


def is_truthy(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def contains_star(node: ast.expr | None) -> bool:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(isinstance(e, ast.Constant) and e.value == "*" for e in node.elts)
    return False


def load_live_deprecations() -> dict[str, str]:
    """
    Prefer the installed package over this file's constant: a deprecation added in 9.1 would
    otherwise go unreported until somebody edited this script.

    This audit is pure AST over *your* code, so it must keep working when `hexcore` is not
    installed in the interpreter running it -- a lint-only CI job, or a look at a project
    before setting it up. `package_root()` raises `SystemExit`, which is a `BaseException`
    and would sail straight through an `except Exception`; catching it is what keeps the
    static table as a real fallback rather than a decorative one.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import hexcore_surface as surface  # type: ignore[import-not-found]

        root = surface.package_root()
        live: dict[str, str] = {}
        for mapping in surface.deprecations(root).values():
            live.update(mapping)
        return live or dict(DEPRECATED_IN_9)
    except (Exception, SystemExit):
        return dict(DEPRECATED_IN_9)


# ---- the auditor -------------------------------------------------------------


@dataclass
class Auditor:
    root: Path
    findings: list[Finding] = field(default_factory=list)
    deprecated: dict[str, str] = field(default_factory=load_live_deprecations)

    # cross-file facts, resolved in finalize()
    background_command_sites: list[tuple[str, int]] = field(default_factory=list)
    retry_middleware_sites: list[tuple[str, int]] = field(default_factory=list)
    bus_factory_sites: list[tuple[str, int, bool]] = field(default_factory=list)
    init_beanie_sites: list[tuple[str, int]] = field(default_factory=list)
    relay_sites: list[tuple[str, int]] = field(default_factory=list)
    saw_publish_after_commit_false: bool = False
    saw_hexcore_import: bool = False

    def add(
        self,
        severity: str,
        rule: str,
        path: Path,
        line: int,
        message: str,
        fix: str,
        doc: str = "",
    ) -> None:
        self.findings.append(
            Finding(severity, rule, self.display(path), line, message, fix, doc)
        )

    def display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # -- entry point --

    def run(self, excludes: tuple[str, ...]) -> list[Finding]:
        for path in sorted(self.root.rglob("*.py")):
            relative = self.display(path)
            if any(fnmatch.fnmatch(relative, pattern) for pattern in excludes):
                continue
            if any(part in {"__pycache__", ".venv", "venv", "node_modules", ".git"} for part in path.parts):
                continue
            try:
                tree = parse_quietly(path.read_text(encoding="utf-8"), str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            self.scan_file(path, tree)
        self.finalize()
        self.findings.sort(key=lambda f: (SEVERITIES.index(f.severity), f.path, f.line))
        return self.findings

    # -- per file --

    def scan_file(self, path: Path, tree: ast.Module) -> None:
        self.mark_parents(tree)
        self.check_imports(path, tree)
        self.check_alembic_env(path, tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self.check_class(path, node)
            elif isinstance(node, ast.Call):
                self.check_call(path, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.check_function(path, node)
            elif isinstance(node, ast.Assign):
                self.check_assign(path, node)

    @staticmethod
    def mark_parents(tree: ast.Module) -> None:
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.hexcore_parent = parent  # type: ignore[attr-defined]

    @staticmethod
    def enclosing_function(node: ast.AST) -> ast.AST | None:
        current = getattr(node, "hexcore_parent", None)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = getattr(current, "hexcore_parent", None)
        return None

    # -- group A: removed and deprecated names --

    def check_imports(self, path: Path, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("hexcore"):
                self.saw_hexcore_import = True
                module = node.module or ""
                for alias in node.names:
                    self.check_name(path, node.lineno, alias.name, module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("hexcore"):
                        self.saw_hexcore_import = True
                        self.check_name(path, node.lineno, alias.name.split(".")[-1], alias.name)

    def check_name(self, path: Path, line: int, name: str, module: str) -> None:
        if name in REMOVED_IN_7:
            self.add(
                "critical",
                "removed-api",
                path,
                line,
                name + " was removed in 7.0 and does not exist in 9.x",
                "use " + REMOVED_IN_7[name],
                "references/removed-api.md",
            )
        elif name in self.deprecated:
            self.add(
                "medium",
                "deprecated-api",
                path,
                line,
                name + " is deprecated and is removed in 10.0",
                "use " + self.deprecated[name],
                "references/removed-api.md",
            )

        if module.startswith("hexcore.infrastructure.events"):
            self.add(
                "medium",
                "deprecated-module",
                path,
                line,
                "the whole hexcore.infrastructure.events package is removed in 10.0",
                "import the bus from hexcore.cqrs instead",
                "references/removed-api.md",
            )

    # -- group B: the silent ones --

    def check_alembic_env(self, path: Path, tree: ast.Module) -> None:
        """
        The worst failure mode in the framework, and the one that does not raise: a table
        that exists in the database and is missing from `Base.metadata` gets an
        `op.drop_table` in the next autogenerated migration. With data in it. The migration
        is generated cleanly and the damage appears when it is applied.
        """
        if path.name != "env.py":
            return
        source = ast.dump(tree)
        if "run_migrations" not in source and "context" not in source:
            return

        called = {call_name(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}

        if "ensure_framework_models_loaded" not in called:
            self.add(
                "critical",
                "alembic-framework-models",
                path,
                1,
                "env.py never calls ensure_framework_models_loaded(), so the framework's own "
                "tables (hexcore_cron_jobs, the event store) are outside Base.metadata",
                "add ensure_framework_models_loaded() before target_metadata is read",
                "references/failure-modes.md#op-drop-table",
            )
        if "import_all_models" not in called:
            self.add(
                "critical",
                "alembic-project-models",
                path,
                1,
                "env.py never calls import_all_models(), so your own models may be missing "
                "from Base.metadata when --autogenerate runs",
                "add import_all_models(<your models package>)",
                "references/failure-modes.md#op-drop-table",
            )
        if "ensure_identity_schema_loaded" not in called:
            self.add(
                "high",
                "alembic-identity-models",
                path,
                1,
                "env.py never calls ensure_identity_schema_loaded(); if this project uses "
                "Darwin, the table --autogenerate drops is the credential store",
                "add ensure_identity_schema_loaded(plugins=[...]) inside a try/except "
                "ImportError, or ignore this if you do not use Darwin",
                "references/darwin.md#alembic",
            )

    def check_class(self, path: Path, node: ast.ClassDef) -> None:
        bases = base_names(node)

        # `@background_command` decorates the Command *class*, not a function -- which is
        # exactly where the first version of this script missed it.
        self.note_background_decorators(path, node)

        # A message class defined inside a function can never be imported by the worker.
        if bases & {"Command", "Query", "DomainEvent"} and self.enclosing_function(node):
            self.add(
                "high",
                "message-in-locals",
                path,
                node.lineno,
                node.name + " is defined inside a function, so its __qualname__ carries "
                "<locals> and the worker cannot import it",
                "move it to module level",
                "references/failure-modes.md#worker-cannot-find-the-message",
            )

        if "BaseEntity" in bases:
            for statement in node.body:
                target = None
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    target = statement.target.id
                elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                    if isinstance(statement.targets[0], ast.Name):
                        target = statement.targets[0].id
                if target in BASE_ENTITY_FIELDS:
                    self.add(
                        "medium",
                        "entity-redeclares-base-field",
                        path,
                        statement.lineno,
                        node.name + " redeclares " + str(target) + ", which BaseEntity already provides",
                        "delete the field",
                        "references/core.md#entities",
                    )

        if bases & GENERIC_REPOSITORIES:
            for statement in node.body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if statement.name in BASE_REPOSITORY_METHODS:
                        self.add(
                            "medium",
                            "repository-reimplements-crud",
                            path,
                            statement.lineno,
                            node.name + "." + statement.name + "() overrides a method the "
                            "generic repository already implements",
                            "delete it unless the override is a deliberate specialisation "
                            "(eager loading, a tenant filter); the base CRUD is not meant to "
                            "be rewritten",
                            "references/core.md#repositories",
                        )

        if "BaseDocument" in bases:
            self.add(
                "medium",
                "consumer-subclasses-basedocument",
                path,
                node.lineno,
                "BaseDocument sets is_root=True (single-collection inheritance) and "
                "use_cache=True, which is rarely what a consumer document wants",
                "subclass beanie.Document directly with your own Settings",
                "references/core.md#beanie",
            )

        if bases & FRAMEWORK_MIXINS and any(b.startswith("BaseModel") for b in bases):
            self.add(
                "medium",
                "framework-table-inherits-basemodel",
                path,
                node.lineno,
                node.name + " mixes a framework table in *and* inherits BaseModel, so the UoW "
                "will ask it for a domain entity it does not have, during commit()",
                "inherit Base, not BaseModel[T]",
                "references/core.md#framework-tables",
            )

        if "AbstractProjection" in bases:
            methods = {
                s.name for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if "apply" in methods and "reset" not in methods:
                self.add(
                    "low",
                    "projection-without-reset",
                    path,
                    node.lineno,
                    node.name + " applies events but implements no reset(), so Projector."
                    "rebuild() cannot clear it",
                    "implement reset()",
                    "references/event-sourcing.md#projections",
                )

        # A column literally named `metadata` shadows `Base.metadata`.
        for statement in node.body:
            target = None
            annotation = None
            value = None
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                target, annotation, value = statement.target.id, statement.annotation, statement.value
            elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                if isinstance(statement.targets[0], ast.Name):
                    target, value = statement.targets[0].id, statement.value
            if target != "metadata":
                continue
            looks_like_column = call_name(value) in {"mapped_column", "Column", "relationship"} or (
                annotation is not None and call_name(annotation) == "Mapped"
            )
            if looks_like_column:
                self.add(
                    "critical",
                    "column-named-metadata",
                    path,
                    statement.lineno,
                    "a column named `metadata` shadows Base.metadata",
                    "rename it (Darwin uses audit_metadata)",
                    "references/failure-modes.md#column-named-metadata",
                )

    def note_background_decorators(
        self, path: Path, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """
        Record `@background_command` for the cross-file rules, and reject anything queued
        that lives inside another function -- the worker resolves it by fully qualified
        name, and `<locals>` is not importable.
        """
        decorators = [call_name(d) for d in node.decorator_list]
        if not any(d.startswith("background_") for d in decorators):
            return

        if "background_command" in decorators:
            self.background_command_sites.append((self.display(path), node.lineno))

        if self.enclosing_function(node):
            self.add(
                "high",
                "task-in-locals",
                path,
                node.lineno,
                node.name + " is decorated for the queue but defined inside another "
                "function, so the worker cannot import it",
                "move it to module level",
                "references/failure-modes.md#worker-cannot-find-the-message",
            )

    def check_function(self, path: Path, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        decorators = [call_name(d) for d in node.decorator_list]
        self.note_background_decorators(path, node)

        # `asyncio.run()` per Celery task closes the loop the AsyncEngine pool is bound to.
        if any(d in {"task", "shared_task"} for d in decorators):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and call_name(inner) == "run":
                    func = inner.func
                    if isinstance(func, ast.Attribute) and call_name(func.value) == "asyncio":
                        self.add(
                            "high",
                            "asyncio-run-in-celery-task",
                            path,
                            inner.lineno,
                            "asyncio.run() inside a Celery task closes the event loop the "
                            "AsyncEngine pool is bound to",
                            "use run_in_worker_loop() from the celery adapter",
                            "references/failure-modes.md#event-loop-is-closed",
                        )

    def check_assign(self, path: Path, node: ast.Assign) -> None:
        """`client = TestClient(app)` outside a `with` never runs the lifespan."""
        if call_name(node.value) != "TestClient":
            return
        parent = getattr(node, "hexcore_parent", None)
        if not isinstance(parent, ast.withitem):
            self.add(
                "medium",
                "testclient-without-with",
                path,
                node.lineno,
                "TestClient(app) outside a `with` block does not run the lifespan, so the "
                "engine is never initialised",
                "use `with TestClient(app) as client:`",
                "references/testing.md#http",
            )

    def check_call(self, path: Path, node: ast.Call) -> None:
        name = call_name(node)

        if name in REMOVED_IN_7:
            self.add(
                "critical",
                "removed-api",
                path,
                node.lineno,
                name + " was removed in 7.0 and does not exist in 9.x",
                "use " + REMOVED_IN_7[name],
                "references/removed-api.md",
            )

        if name == "ServerConfig" or name == "LazyConfig":
            self.check_server_config(path, node)

        if name == "TransactionMiddleware" and keyword_of(node, "uow_factory") is None:
            self.add(
                "high",
                "transaction-middleware-without-factory",
                path,
                node.lineno,
                "TransactionMiddleware() without a uow_factory raises ValueError at "
                "construction",
                "pass uow_factory=lambda: SqlAlchemyUnitOfWork(session=session_factory()) -- "
                "and only for handlers that do not manage their own transaction",
                "references/cqrs-workers-cron.md#middleware",
            )

        if name == "RetryMiddleware":
            self.retry_middleware_sites.append((self.display(path), node.lineno))

        if name in {"CQRSFactory", "configure_cqrs"}:
            has_enqueuer = keyword_of(node, "enqueuer") is not None
            self.bus_factory_sites.append((self.display(path), node.lineno, has_enqueuer))

        if name in {"init_beanie", "init_beanie_documents", "BeanieStep"}:
            self.init_beanie_sites.append((self.display(path), node.lineno))

        if name == "EventStoreRelay":
            self.relay_sites.append((self.display(path), node.lineno))

        if name == "SqlAlchemyUnitOfWork":
            keyword = keyword_of(node, "publish_after_commit")
            if keyword is not None and isinstance(keyword.value, ast.Constant):
                if keyword.value.value is False:
                    self.saw_publish_after_commit_false = True

        if name == "async_sessionmaker" and keyword_of(node, "expire_on_commit") is None:
            self.add(
                "medium",
                "sessionmaker-without-expire-on-commit",
                path,
                node.lineno,
                "async_sessionmaker without expire_on_commit=False: attributes expire on "
                "commit and the next access lazy-loads on a closed session",
                "pass expire_on_commit=False, or use sql.get_session_factory()",
                "references/failure-modes.md#missinggreenlet",
            )

        if name == "cron_job" and node.args and isinstance(node.args[0], ast.Constant):
            self.add(
                "low",
                "cron-job-task-name-by-hand",
                path,
                node.lineno,
                "cron_job() was given a task name as a string instead of the decorated "
                "function, so a rename will not be caught here",
                "pass the function; the name comes from __cqrs_task_name__",
                "references/cqrs-workers-cron.md#cron",
            )

        if name in {"dispatch_events", "collect_domain_events", "collect_domain_entities"}:
            self.add(
                "medium",
                "manual-event-dispatch",
                path,
                node.lineno,
                name + "() is the Unit of Work's job at commit time",
                "delete the call; uow.commit() collects and publishes",
                "references/core.md#events",
            )

    def check_server_config(self, path: Path, node: ast.Call) -> None:
        origins = keyword_of(node, "allow_origins")
        credentials = keyword_of(node, "allow_credentials")

        if origins is not None and contains_star(origins.value):
            if credentials is not None and is_truthy(credentials.value):
                self.add(
                    "high",
                    "cors-star-with-credentials",
                    path,
                    node.lineno,
                    'allow_origins=["*"] with allow_credentials=True is never valid: the app '
                    "refuses to start, and if it did, Starlette would reflect the attacker's "
                    "Origin with Access-Control-Allow-Credentials: true",
                    "declare your origins explicitly",
                    "references/failure-modes.md#cors",
                )
            else:
                self.add(
                    "low",
                    "cors-star",
                    path,
                    node.lineno,
                    'allow_origins=["*"] forces allow_credentials down to False, so cookie '
                    "authentication will silently not work",
                    "declare your origins explicitly if you use session cookies",
                    "references/failure-modes.md#cors",
                )

        if call_name(node) == "ServerConfig" and keyword_of(node, "repository_discovery_paths") is None:
            self.add(
                "high",
                "no-repository-discovery-paths",
                path,
                node.lineno,
                "ServerConfig without repository_discovery_paths: discovery is explicit since "
                "v2, and the Unit of Work fails to build when the set is empty",
                "pass repository_discovery_paths={...} with your repository modules",
                "references/core.md#configuration",
            )

    # -- cross-file --

    def finalize(self) -> None:
        if self.background_command_sites:
            for path, line in self.retry_middleware_sites:
                self.findings.append(
                    Finding(
                        "high",
                        "retry-multiplies-with-queue",
                        path,
                        line,
                        "RetryMiddleware alongside @background_command: the queue retries and "
                        "the middleware retries inside each attempt, so 3 x 3 is up to 12 "
                        "executions of a possibly non-idempotent handler",
                        "pick one -- the queue's retry for background commands, the "
                        "middleware for synchronous ones",
                        "references/failure-modes.md#handler-runs-12-times",
                    )
                )
            for path, line, has_enqueuer in self.bus_factory_sites:
                if not has_enqueuer:
                    self.findings.append(
                        Finding(
                            "high",
                            "bus-without-enqueuer",
                            path,
                            line,
                            "the registry holds @background_command handlers and this bus was "
                            "built without an enqueuer",
                            "pass enqueuer=... -- CQRSFactory fails at construction rather "
                            "than on the first dispatch, with a user request in flight",
                            "references/failure-modes.md#runtimeerror-on-first-dispatch",
                        )
                    )

        if len(self.init_beanie_sites) > 1:
            for path, line in self.init_beanie_sites:
                self.findings.append(
                    Finding(
                        "high",
                        "init-beanie-called-twice",
                        path,
                        line,
                        "init_beanie does not accumulate: a second call against the same "
                        "database replaces the first call's registry, and a Document it never "
                        "saw fails with CollectionWasNotInitialized",
                        "put every document -- yours, identity's, the plugins' -- in one call",
                        "references/failure-modes.md#collectionwasnotinitialized",
                    )
                )

        if self.relay_sites and not self.saw_publish_after_commit_false:
            for path, line in self.relay_sites:
                self.findings.append(
                    Finding(
                        "high",
                        "relay-and-uow-both-publish",
                        path,
                        line,
                        "an EventStoreRelay is running and no Unit of Work was built with "
                        "publish_after_commit=False, so every event goes out twice, silently",
                        "pass publish_after_commit=False to the UoW",
                        "references/event-sourcing.md#outbox",
                    )
                )


# ---- output ------------------------------------------------------------------

_LABEL = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
}


def report(findings: list[Finding], as_json: bool, scanned: Path) -> None:
    if as_json:
        print(json.dumps([f.as_dict() for f in findings], indent=2))
        return

    if not findings:
        print("No findings in " + str(scanned) + ".")
        return

    counts = {level: sum(1 for f in findings if f.severity == level) for level in SEVERITIES}
    summary = ", ".join(
        str(counts[level]) + " " + level for level in SEVERITIES if counts[level]
    )
    print(str(len(findings)) + " finding(s) in " + str(scanned) + ": " + summary + "\n")

    current = ""
    for finding in findings:
        if finding.severity != current:
            current = finding.severity
            print("-- " + _LABEL[current] + " " + "-" * (60 - len(_LABEL[current])))
        print("  " + finding.path + ":" + str(finding.line) + "  [" + finding.rule + "]")
        print("      " + finding.message)
        print("      fix: " + finding.fix)
        if finding.doc:
            print("      see: " + finding.doc)
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a project that consumes HexCore.")
    parser.add_argument("path", nargs="?", default=".", help="directory to audit (default: .)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--fail-on",
        choices=(*SEVERITIES, "none"),
        default="high",
        help="exit non-zero at this severity or worse (default: high)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching this glob; repeatable",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.exists():
        print("no such path: " + str(root), file=sys.stderr)
        return 2

    auditor = Auditor(root)
    findings = auditor.run(tuple(args.exclude))
    report(findings, args.json, root)

    if not auditor.saw_hexcore_import and not findings and not args.json:
        print("(no hexcore imports found -- is this a HexCore project?)")

    if args.fail_on == "none":
        return 0
    threshold = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f.severity) <= threshold for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
