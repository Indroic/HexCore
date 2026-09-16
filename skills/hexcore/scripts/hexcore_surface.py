#!/usr/bin/env python3
"""
Read HexCore's real public surface out of the installed package.

Why this exists
---------------
The previous version of this skill carried a hand-written table of import paths. It went
stale across seven majors: it still taught `SQLAlchemyCommonImplementationsRepo`, which was
deleted in 7.0. A written table cannot track a package that releases a major from a `feat!:`
commit -- which HexCore has done twice by accident, per its own migration guide.

So this does not carry the surface. It reads it.

All five facades (`hexcore.cqrs`, `hexcore.sql`, `hexcore.fastapi`, `hexcore.eventsourcing`,
`hexcore.darwin`) share one shape:

    _EXPORTS: dict[str, tuple[str, str]] = {"init_engine": ("hexcore...session", "init_engine")}
    __all__ = sorted(_EXPORTS)
    def __getattr__(name): ...      # PEP 562, resolves lazily

`_EXPORTS` is a top-level dict literal, so `ast.literal_eval` reads it **without importing
anything**. That matters more than it looks: it works on a bare install with zero extras,
never triggers the import of half the framework, and cannot fail because the consumer picked
Mongo over SQL.

Deprecations are introspectable the same way. They are all declared as

    __getattr__ = deprecated_lazy_names(__name__, {"EventBus": "...AbstractEventBus"}, ...)

so the old -> new map is a dict literal too, and this script builds the deprecation table
from the package instead of from memory.

Usage
-----
    python hexcore_surface.py --version
    python hexcore_surface.py --facade cqrs
    python hexcore_surface.py --find SqlAlchemyRepository
    python hexcore_surface.py --check SKILL.md references/
    python hexcore_surface.py --deprecated
    python hexcore_surface.py --registry
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import warnings
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: The major this skill's prose documents. `--version` compares against it.
DOCUMENTED_MAJOR = "9"

# ---- Where the surface lives -------------------------------------------------

#: facade name -> path of its module inside the package
FACADES: dict[str, str] = {
    "cqrs": "cqrs.py",
    "sql": "sql.py",
    "fastapi": "fastapi.py",
    "eventsourcing": "eventsourcing.py",
    "darwin": "darwin/__init__.py",
}

#: The aliases HexCore's own documentation uses for each facade. `hx` for `hexcore.fastapi`
#: and `es` for `hexcore.eventsourcing` are what the guides teach, so `--check` has to
#: understand them to verify an example.
FACADE_ALIASES: dict[str, str] = {
    "hx": "fastapi",
    "cqrs": "cqrs",
    "sql": "sql",
    "es": "eventsourcing",
    "darwin": "darwin",
}

#: The extra a whole facade needs, per `docs/en/reference.md`. `cqrs` and `eventsourcing`
#: need none: their ports and in-memory adapters run on a bare install, and each backend
#: demands its own extra at the moment you ask for the name.
FACADE_BASE_EXTRA: dict[str, str | None] = {
    "cqrs": None,
    "eventsourcing": None,
    "sql": "sql",
    "fastapi": "api",
    "darwin": "darwin",
}

#: Substring of the origin module -> the extra that provides it. Ordered: first match wins,
#: so Darwin and the specific storage cases are decided before the bare `sqlalchemy`. The
#: criterion is the one `hexcore.capabilities.require_extra` applies at runtime.
_EXTRA_MODULE_RULES: tuple[tuple[str, str], ...] = (
    ("darwin", "darwin"),
    ("orms.beanie", "mongo"),
    ("beanie", "mongo"),
    ("orms.sqlalchemy", "sql"),
    ("cron_sql", "sql"),
    ("postgres", "sql"),
    ("sqlalchemy", "sql"),
    ("rabbitmq", "rabbitmq"),
    ("procrastinate", "procrastinate"),
    ("celery", "celery"),
    ("redis", "redis"),
    ("infrastructure.api", "api"),
    ("fastapi", "api"),
)

#: Substring of the *symbol name* -> extra. Needed because some facade entries point at a
#: dispatcher module rather than at the backend: `SqlAlchemyRepository` lives in
#: `...repositories.implementations`, which names no driver, and it still needs `[sql]`.
_EXTRA_NAME_RULES: tuple[tuple[str, str], ...] = (
    ("sqlalchemy", "sql"),
    ("beanie", "mongo"),
    ("rabbitmq", "rabbitmq"),
    ("procrastinate", "procrastinate"),
    ("celery", "celery"),
    ("redis", "redis"),
    ("postgres", "sql"),
)


def extra_for(module_path: str, name: str = "", facade: str | None = None) -> str | None:
    """
    The extra a symbol needs: where it lives, then what it is called, then its facade's
    baseline. Three signals because no single one is complete -- the module is silent for
    lazily dispatched names, and the facade baseline is too coarse for `hexcore.cqrs`,
    where only the SQL and Redis entries need anything at all.
    """
    lowered_module = module_path.lower()
    for needle, extra in _EXTRA_MODULE_RULES:
        if needle in lowered_module:
            return extra

    lowered_name = name.lower()
    for needle, extra in _EXTRA_NAME_RULES:
        if needle in lowered_name:
            return extra

    return FACADE_BASE_EXTRA.get(facade or "", None)


# ---- Locating the package ----------------------------------------------------


def package_root(override: str | None = None) -> Path:
    """
    The `hexcore/` directory of the installed package.

    `find_spec` rather than `import hexcore`: asking where something is should not execute
    it. Same reasoning `hexcore.capabilities.has_extra` documents for itself.
    """
    if override:
        root = Path(override).resolve()
        if (root / "__init__.py").exists():
            return root
        if (root / "hexcore" / "__init__.py").exists():
            return root / "hexcore"
        raise SystemExit("no hexcore package at " + str(root))

    import importlib.util

    spec = importlib.util.find_spec("hexcore")
    if spec is None or not spec.origin:
        raise SystemExit(
            "hexcore is not installed in this interpreter.\n"
            "    pip install hexcore\n"
            "or point at a checkout with --package PATH"
        )
    return Path(spec.origin).parent


def installed_version(root: Path) -> str:
    """The installed version, with a checkout fallback so this works on a clone too."""
    try:
        import importlib.metadata as md

        return md.version("hexcore")
    except Exception:
        pyproject = root.parent / "pyproject.toml"
        if pyproject.exists():
            match = re.search(
                r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M
            )
            if match:
                return match.group(1)
        return "unknown"


# ---- Reading the surface, without importing it -------------------------------


def _module_ast(path: Path) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def facade_exports(root: Path, facade: str) -> dict[str, tuple[str, str]]:
    """`_EXPORTS` of one facade: symbol -> (origin module, attribute)."""
    path = root / FACADES[facade]
    if not path.exists():
        raise SystemExit("facade " + facade + " not found at " + str(path))

    for node in _module_ast(path).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "_EXPORTS" for t in targets):
            if node.value is None:
                continue
            raw = ast.literal_eval(node.value)
            return {key: tuple(value) for key, value in raw.items()}

    raise SystemExit(str(path) + " has no top-level _EXPORTS -- the facade shape changed")


def all_facades(root: Path) -> dict[str, dict[str, tuple[str, str]]]:
    return {name: facade_exports(root, name) for name in FACADES}


def module_file(root: Path, dotted: str) -> Path | None:
    """`hexcore.infrastructure.uow` -> the .py backing it, package or module."""
    if dotted == "hexcore":
        return root / "__init__.py"
    if not dotted.startswith("hexcore."):
        return None
    relative = dotted[len("hexcore.") :].replace(".", "/")
    for candidate in (root / (relative + ".py"), root / relative / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def module_names(path: Path) -> set[str]:
    """
    Every top-level name a module offers: `__all__` when it declares a literal one, plus
    what it actually binds -- classes, functions, assignments and re-exported imports.

    Both are needed. A facade declares `__all__ = sorted(_EXPORTS)`, a runtime expression
    that evaluates to nothing useful here; a plain module may declare no `__all__` at all
    and still be imported from.
    """
    tree = _module_ast(path)
    names: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                    if target.id == "__all__":
                        try:
                            names.update(ast.literal_eval(node.value))
                        except (ValueError, SyntaxError):
                            pass
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.If):
            # `if t.TYPE_CHECKING:` blocks re-export names for type checkers.
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom):
                    for alias in inner.names:
                        names.add(alias.asname or alias.name)
                elif isinstance(inner, ast.Assign):
                    for target in inner.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)

    return names


def dotted_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(["hexcore", *parts])


def deprecations(root: Path) -> dict[str, dict[str, str]]:
    """
    Every deprecated name in the package: module -> {old name: replacement}.

    Built by finding the `deprecated_lazy_names(...)` / `deprecated_aliases(...)` calls and
    reading their second positional argument, a dict literal by construction. The package
    documents the idiom in `hexcore/_deprecation.py`.
    """
    found: dict[str, dict[str, str]] = {}
    wanted = {"deprecated_lazy_names", "deprecated_aliases"}

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = _module_ast(path)
        except (SyntaxError, UnicodeDecodeError):
            continue

        # The map is sometimes a literal at the call site and sometimes a module-level
        # constant passed by name -- `hexcore.domain.auth` declares `_DEPRECADOS` and hands
        # it over. Resolving top-level literals first covers both without importing.
        constants: dict[str, dict[str, str]] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(value, dict):
                        constants[target.id] = value

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in wanted or len(node.args) < 2:
                continue
            argument = node.args[1]
            if isinstance(argument, ast.Name):
                mapping = constants.get(argument.id)
            else:
                try:
                    mapping = ast.literal_eval(argument)
                except (ValueError, SyntaxError):
                    continue
            if isinstance(mapping, dict) and mapping:
                dotted = dotted_name(root, path)
                found.setdefault(dotted, {}).update(
                    {str(k): str(v) for k, v in mapping.items()}
                )

    return found


# ---- The TypeScript client -----------------------------------------------------
#
# `@hexcore-js/darwin-client` is the second publishable package of the same repository, and
# the skill documents it too. Its surface is read the same way the Python one is -- from the
# source, not from a table here -- because it has the same failure mode: a table would go
# stale at the next rename and nobody would notice until an agent emitted an import that does
# not exist.
#
# Why `src/*.ts` and not `dist/*.d.ts`: `dist/` is gitignored, so it does not exist in a clean
# checkout or in CI. The three entry points are flat lists of re-exports, which a regex reads
# correctly. `dist/` is still used as a fallback, because in a *consumer* project the package
# arrives from npm with no `src/` at all.

#: subpath as it is imported -> the source file that defines it, relative to the package.
DARWIN_CLIENT_SUBPATHS: dict[str, tuple[str, str]] = {
    ".": ("src/index.ts", "dist/index.d.ts"),
    "./webauthn": ("src/webauthn.ts", "dist/webauthn.d.ts"),
    "./store": ("src/store.ts", "dist/store.d.ts"),
}

DARWIN_CLIENT_PACKAGE = "@hexcore-js/darwin-client"

#: This skill's own tooling, skipped when `--check` walks a directory. Named explicitly and
#: not by path so it works whether the skill lives in `skills/` or in `.claude/skills/`.
SELF = frozenset({"hexcore_surface.py", "hexcore_audit.py"})

#: `export { a, b as c } from "./x"` and `export type { A } from "./y"`, braces possibly
#: spanning lines.
_TS_EXPORT_BLOCK = re.compile(r"\bexport\s+(?:type\s+)?\{([^}]*)\}", re.S)

#: `export function foo`, `export class Foo`, `export const foo`, `export interface Foo`, ...
_TS_EXPORT_DECL = re.compile(
    r"\bexport\s+(?:declare\s+)?(?:async\s+)?"
    r"(?:function|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)"
)


def darwin_client_root(hexcore_root: Path | None = None, override: str | None = None) -> Path | None:
    """
    Where `@hexcore-js/darwin-client` is, or `None` if it is not reachable from here.

    `None` rather than an exception: the client is optional. Somebody auditing a pure-backend
    project has no reason to have it around, and refusing to run `--check` over the Python
    prose because a TypeScript package is missing would be the tool getting in the way.
    """
    if override:
        root = Path(override).resolve()
        if (root / "package.json").exists():
            return root
        raise SystemExit("no package.json at " + str(root))

    candidates: list[Path] = []
    if hexcore_root is not None:
        # The monorepo sibling: `packages/hexcore/hexcore` -> `packages/darwin-client`.
        candidates.append(hexcore_root.parent.parent / "darwin-client")

    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for directory in (start, *start.parents):
            candidates.append(directory / "packages" / "darwin-client")
            candidates.append(directory / "node_modules" / "@hexcore-js" / "darwin-client")

    for candidate in candidates:
        if (candidate / "package.json").exists():
            return candidate
    return None


#: Comments first. This package's docstrings are long and carry runnable examples --
#: `export const session = toSvelteStore(client.session)` is documentation, not an export,
#: and counting it would make the checker accept a name the package does not have.
_TS_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _ts_exported_names(text: str) -> set[str]:
    """The names a TypeScript module exports. `export { a as b }` exports `b`, not `a`."""
    text = _TS_COMMENT.sub("", text)
    names: set[str] = set()
    for body in _TS_EXPORT_BLOCK.findall(text):
        for raw in body.split(","):
            piece = raw.strip()
            if not piece:
                continue
            piece = piece.split(" as ")[-1].strip()
            piece = piece.removeprefix("type ").strip()
            if piece.isidentifier():
                names.add(piece)
    names.update(_TS_EXPORT_DECL.findall(text))
    return names


def darwin_client_exports(root: Path) -> dict[str, set[str]]:
    """subpath -> the names it exports. Missing entry points are simply absent."""
    found: dict[str, set[str]] = {}
    for subpath, (source, built) in DARWIN_CLIENT_SUBPATHS.items():
        for relative in (source, built):
            path = root / relative
            if path.exists():
                found[subpath] = _ts_exported_names(path.read_text(encoding="utf-8"))
                break
    return found


def darwin_client_version(root: Path) -> tuple[str, str]:
    """`(package version, the Darwin contract the generated types came from)`."""
    version = "unknown"
    manifest = root / "package.json"
    if manifest.exists():
        match = re.search(r'"version"\s*:\s*"([^"]+)"', manifest.read_text(encoding="utf-8"))
        if match:
            version = match.group(1)

    contract = "unknown"
    stamp = root / "openapi" / "VERSION"
    if stamp.exists():
        contract = stamp.read_text(encoding="utf-8").strip() or "unknown"
    return version, contract


# ---- --check: does what this skill teaches actually resolve? ------------------


@dataclass
class Finding:
    path: str
    line: int
    symbol: str
    status: str  # "missing" | "deprecated" | "unknown-module"
    detail: str


CODE_BLOCK = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
IMPORT_LINE = re.compile(r"^[ \t]*from[ \t]+(hexcore[\w.]*)[ \t]+import[ \t]+([^\n(#]+)", re.M)
FACADE_USE = re.compile(r"(?<![\w./])(hx|cqrs|sql|es|darwin)\.([A-Za-z_][A-Za-z0-9_]*)\b")

#: TypeScript examples are fenced with their own language tag, so they are separable from the
#: Python ones without guessing: the same file can teach both sides of an integration and each
#: block gets checked against the package it actually belongs to.
TS_BLOCK = re.compile(r"```(?:ts|tsx|typescript|javascript|js)\n(.*?)```", re.DOTALL)

#: `import { a, b } from "@hexcore-js/darwin-client/webauthn"`, braces possibly over lines.
CLIENT_IMPORT = re.compile(
    r"import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*[\"'](@hexcore-js/darwin-client(?:/[\w-]+)?)[\"']",
    re.S,
)

#: `sql.md`, `cqrs.py`, `darwin.pyi` are filenames, not facade attributes. HexCore's own
#: documentation gate had to special-case exactly this once the docs moved into `docs/en/`.
_FILE_SUFFIXES = {"md", "py", "pyi", "toml", "json", "yaml", "yml", "txt", "html"}


def _facade_for_module(dotted: str) -> str | None:
    """A facade asked for by its long path is still the facade."""
    if dotted == "hexcore.darwin":
        return "darwin"
    parts = dotted.split(".")
    if len(parts) == 2 and parts[1] in FACADES:
        return parts[1]
    return None


@dataclass
class Checker:
    root: Path
    facades: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    deprecated: dict[str, dict[str, str]] = field(default_factory=dict)
    #: subpath -> exported names of `@hexcore-js/darwin-client`. Empty when the package is not
    #: reachable: a TypeScript block then goes unchecked rather than reported as broken, which
    #: is the honest answer when the tool has nothing to compare against.
    client: dict[str, set[str]] = field(default_factory=dict)
    _names: dict[str, set[str] | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.facades:
            self.facades = all_facades(self.root)
        if not self.deprecated:
            self.deprecated = deprecations(self.root)

    def names_of(self, dotted: str) -> set[str] | None:
        if dotted not in self._names:
            path = module_file(self.root, dotted)
            self._names[dotted] = module_names(path) if path else None
        return self._names[dotted]

    def check_text(self, text: str, label: str, *, code_only: bool) -> list[Finding]:
        """
        In markdown only code blocks are checked: prose has to be free to name the old API,
        because the removed-API reference exists precisely to name it. What must not happen
        is an *example* -- the thing people copy -- teaching something that does not exist.
        That is the same split HexCore's own documentation gate makes.
        """
        findings: list[Finding] = []
        if code_only:
            segments = [(block, text.find(block)) for block in CODE_BLOCK.findall(text)]
        else:
            segments = [(text, 0)]

        # Facade attributes are checked over the **whole** markdown, not just its code
        # blocks. Prose has to stay free to name a deleted symbol -- `SqlAlchemyRepository`
        # bare, `ServerConfig(event_dispatcher=...)` -- and none of those carry a facade
        # prefix, so `cqrs.Foo` in a table is still a claim this can verify.
        if code_only:
            findings.extend(self._check_facade_uses(text, label, 0))

        for segment, base in segments:
            prefix_lines = text[:base].count("\n") if base > 0 else 0

            for match in IMPORT_LINE.finditer(segment):
                line = prefix_lines + segment[: match.start()].count("\n") + 1
                dotted, raw_names = match.group(1), match.group(2)
                facade = _facade_for_module(dotted)
                known = set(self.facades[facade]) if facade else self.names_of(dotted)

                if known is None:
                    findings.append(
                        Finding(label, line, dotted, "unknown-module", "module does not exist")
                    )
                    continue

                for raw in raw_names.split(","):
                    name = raw.strip().split(" as ")[0].strip()
                    if not name or not name.isidentifier():
                        continue
                    replacement = self.deprecated.get(dotted, {}).get(name)
                    if replacement:
                        findings.append(
                            Finding(
                                label, line, dotted + "." + name, "deprecated", "use " + replacement
                            )
                        )
                    elif name not in known:
                        findings.append(
                            Finding(label, line, dotted + "." + name, "missing", "not exported")
                        )

            if not code_only:
                findings.extend(self._check_facade_uses(segment, label, prefix_lines))

        findings.extend(self._check_client_imports(text, label, fenced=code_only))
        return findings

    def _check_facade_uses(self, text: str, label: str, prefix_lines: int) -> list[Finding]:
        findings: list[Finding] = []
        for match in FACADE_USE.finditer(text):
            alias, attribute = match.group(1), match.group(2)
            if attribute in _FILE_SUFFIXES:
                continue
            facade = FACADE_ALIASES[alias]
            if attribute not in self.facades[facade]:
                line = prefix_lines + text[: match.start()].count("\n") + 1
                findings.append(
                    Finding(
                        label,
                        line,
                        alias + "." + attribute,
                        "missing",
                        "hexcore." + facade + " does not export it",
                    )
                )
        return findings

    def _check_client_imports(self, text: str, label: str, *, fenced: bool) -> list[Finding]:
        """
        Every `import { … } from "@hexcore-js/darwin-client"` resolved against the package.

        In markdown only the ```ts blocks are read, for the same reason the Python half only
        reads code blocks: prose has to stay free to name something in order to say it is
        wrong. In a `.ts` file the whole text is the example.
        """
        if not self.client:
            return []

        findings: list[Finding] = []
        if fenced:
            segments = [(block, text.find(block)) for block in TS_BLOCK.findall(text)]
        else:
            segments = [(text, 0)]

        for segment, base in segments:
            prefix_lines = text[:base].count("\n") if base > 0 else 0
            for match in CLIENT_IMPORT.finditer(segment):
                line = prefix_lines + segment[: match.start()].count("\n") + 1
                raw_names, module = match.group(1), match.group(2)
                subpath = "." + module[len(DARWIN_CLIENT_PACKAGE) :]
                known = self.client.get(subpath)
                if known is None:
                    findings.append(
                        Finding(
                            label,
                            line,
                            module,
                            "unknown-module",
                            "the package publishes " + ", ".join(sorted(self.client)),
                        )
                    )
                    continue
                for raw in raw_names.split(","):
                    name = raw.strip().removeprefix("type ").strip()
                    name = name.split(" as ")[0].strip()
                    if not name or not name.isidentifier():
                        continue
                    if name not in known:
                        findings.append(
                            Finding(
                                label,
                                line,
                                module + "." + name,
                                "missing",
                                "not exported by " + module,
                            )
                        )
        return findings

    def check_file(self, path: Path) -> list[Finding]:
        text = path.read_text(encoding="utf-8")
        code_only = path.suffix.lower() in {".md", ".markdown"}
        return self.check_text(text, str(path), code_only=code_only)


# ---- Output ------------------------------------------------------------------


def print_version(root: Path) -> int:
    version = installed_version(root)
    major = version.split(".")[0]
    print("hexcore " + version + "   (" + str(root) + ")")
    if major == DOCUMENTED_MAJOR:
        print("This skill documents " + DOCUMENTED_MAJOR + ".x. You are on it.")
        return 0
    if major.isdigit() and int(major) < int(DOCUMENTED_MAJOR):
        print(
            "\n!! This skill documents " + DOCUMENTED_MAJOR + ".x, the project is on " + version + ".\n"
            "   Read references/removed-api.md before writing anything: names this skill\n"
            "   teaches may not exist yet in that version."
        )
        return 1
    print(
        "\n!! This skill documents " + DOCUMENTED_MAJOR + ".x, the project is on " + version + ".\n"
        "   Trust --facade/--find over the reference tables: those read this package."
    )
    return 1


def print_facade(root: Path, facade: str, as_json: bool) -> int:
    exports = facade_exports(root, facade)
    if as_json:
        payload = {k: {"module": m, "attribute": a} for k, (m, a) in exports.items()}
        print(json.dumps(payload, indent=2))
        return 0
    print("hexcore." + facade + " -- " + str(len(exports)) + " symbols\n")
    width = max(len(k) for k in exports)
    for name in sorted(exports):
        module, attribute = exports[name]
        extra = extra_for(module, name, facade)
        suffix = "  [" + extra + "]" if extra else ""
        print("  " + name.ljust(width) + "  " + module + "." + attribute + suffix)
    return 0


def print_darwin_client(root: Path | None, as_json: bool) -> int:
    if root is None:
        print(
            "@hexcore-js/darwin-client is not reachable from here.\n"
            "    Inside the HexCore monorepo it is packages/darwin-client.\n"
            "    In a consumer project, `npm install @hexcore-js/darwin-client`,\n"
            "    or point at it with --darwin-client-package PATH."
        )
        return 1

    version, contract = darwin_client_version(root)
    exports = darwin_client_exports(root)

    if as_json:
        print(
            json.dumps(
                {
                    "version": version,
                    "contract": contract,
                    "path": str(root),
                    "exports": {k: sorted(v) for k, v in exports.items()},
                },
                indent=2,
            )
        )
        return 0

    print(DARWIN_CLIENT_PACKAGE + " " + version + "   (" + str(root) + ")")
    print("generated from the Darwin contract of hexcore " + contract + "\n")
    for subpath in sorted(exports):
        names = exports[subpath]
        label = DARWIN_CLIENT_PACKAGE if subpath == "." else DARWIN_CLIENT_PACKAGE + subpath[1:]
        print('import { ... } from "' + label + '"  -- ' + str(len(names)) + " symbols\n")
        for name in sorted(names):
            print("  " + name)
        print()
    return 0


def _alias_for(facade: str) -> str:
    for alias, target in FACADE_ALIASES.items():
        if target == facade:
            return alias
    return facade


def print_find(root: Path, needle: str, client_root: Path | None = None) -> int:
    """
    Both packages, one command. An agent that has to know which package a name belongs to
    *before* it can ask where it lives is an agent that guesses, and guessing is the failure
    this whole script exists to remove.
    """
    hits: list[str] = []
    for facade, exports in all_facades(root).items():
        for name, (module, attribute) in exports.items():
            if needle.lower() in name.lower():
                extra = extra_for(module, name, facade)
                hits.append(
                    "  " + name + "\n"
                    "      facade   import hexcore." + facade + "  ->  " + _alias_for(facade) + "." + name + "\n"
                    "      long     from " + module + " import " + attribute + "\n"
                    "      extra    " + ("[" + extra + "]" if extra else "(none)")
                )

    for module, mapping in deprecations(root).items():
        for old, new in mapping.items():
            if needle.lower() in old.lower():
                hits.append("  " + old + "   ** DEPRECATED ** in " + module + "\n      use      " + new)

    if client_root is not None:
        for subpath, names in darwin_client_exports(client_root).items():
            label = (
                DARWIN_CLIENT_PACKAGE
                if subpath == "."
                else DARWIN_CLIENT_PACKAGE + subpath[1:]
            )
            for name in names:
                if needle.lower() in name.lower():
                    firma = '      client   import { ' + name + ' } from "' + label + '"'
                    hits.append(
                        "  " + name + "\n"
                        + firma + "\n"
                        + "      package  " + DARWIN_CLIENT_PACKAGE + " (TypeScript)"
                    )

    if not hits:
        print("no symbol matching " + repr(needle) + " in either package.")
        print("It may live on a module without a facade -- see references/core.md.")
        return 1
    print("\n\n".join(sorted(hits)))
    return 0


def print_deprecated(root: Path) -> int:
    found = deprecations(root)
    if not found:
        print("nothing deprecated in this version.")
        return 0
    for module in sorted(found):
        print(module)
        for old, new in sorted(found[module].items()):
            print("    " + old + "  ->  " + new)
    return 0


def print_registry(root: Path) -> int:
    """The import registry, as markdown. This is what regenerates the reference tables."""
    version = installed_version(root)
    print("<!-- generated by scripts/hexcore_surface.py --registry, hexcore " + version + " -->\n")
    for facade, exports in all_facades(root).items():
        print("### `hexcore." + facade + "` -- " + str(len(exports)) + " symbols\n")
        print("| Symbol | Canonical path | Extra |")
        print("| :-- | :-- | :-- |")
        for name in sorted(exports):
            module, attribute = exports[name]
            extra = extra_for(module, name, facade)
            cell = "`[" + extra + "]`" if extra else "--"
            print("| `" + name + "` | `" + module + "." + attribute + "` | " + cell + " |")
        print()
    return 0


def report(findings: list[Finding], as_json: bool) -> int:
    if as_json:
        print(json.dumps([f.__dict__ for f in findings], indent=2))
        return 1 if findings else 0
    if not findings:
        print("OK -- every hexcore symbol resolves against the installed package.")
        return 0
    print(str(len(findings)) + " problem(s):\n")
    for f in findings:
        print("  " + f.path + ":" + str(f.line) + "  [" + f.status + "]  " + f.symbol)
        print("      " + f.detail)
    return 1


# ---- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read HexCore's public surface from the installed package."
    )
    parser.add_argument("--package", help="path to a hexcore checkout, instead of the installed one")
    parser.add_argument(
        "--darwin-client-package",
        help="path to the @hexcore-js/darwin-client package, if it is not next to hexcore",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--version", action="store_true", help="installed version, and whether this skill covers it")
    group.add_argument("--facade", choices=sorted(FACADES), help="every symbol of one facade")
    group.add_argument("--find", metavar="NAME", help="where a symbol lives, and what extra it needs")
    group.add_argument("--check", nargs="+", metavar="PATH", help="verify every hexcore import in these files or dirs")
    group.add_argument("--deprecated", action="store_true", help="the deprecation table, read from the package")
    group.add_argument("--registry", action="store_true", help="the whole import registry, as markdown")
    group.add_argument("--darwin-client", action="store_true", help="what the TypeScript client exports")

    args = parser.parse_args(argv)
    root = package_root(args.package)
    client_root = darwin_client_root(root, args.darwin_client_package)

    if args.darwin_client:
        return print_darwin_client(client_root, args.json)
    if args.version:
        return print_version(root)
    if args.facade:
        return print_facade(root, args.facade, args.json)
    if args.find:
        return print_find(root, args.find, client_root)
    if args.deprecated:
        return print_deprecated(root)
    if args.registry:
        return print_registry(root)

    checker = Checker(
        root, client=darwin_client_exports(client_root) if client_root else {}
    )
    findings: list[Finding] = []
    for raw in args.check:
        path = Path(raw)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                # The two scripts are the checker, not the teaching. Their docstrings show
                # deliberately broken examples (`cqrs.Foo`, `import { a, b }`) to explain what
                # they detect, and reporting those as findings would mean `--check` over the
                # skill's own directory never comes back clean.
                if child.name in SELF:
                    continue
                if child.suffix.lower() in {".md", ".py", ".ts", ".tsx"}:
                    findings.extend(checker.check_file(child))
        elif path.exists():
            findings.extend(checker.check_file(path))
        else:
            print("skipped (not found): " + raw, file=sys.stderr)
    return report(findings, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
