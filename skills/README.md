# Agent skills

Skills for [Claude Code](https://claude.com/claude-code) that ship with this repository, so
they version with the packages they document instead of drifting from them in a repo of their
own.

| Skill | Covers |
| :-- | :-- |
| [`hexcore/`](./hexcore) | Both packages: the Python framework and `@hexcore-js/darwin-client` |

## How they load

The repository root is itself a Claude Code plugin: `.claude-plugin/plugin.json` declares it,
`.claude-plugin/marketplace.json` publishes it, and `.claude/settings.json` enables it. Because
`skills/` is the default location a plugin looks in, nothing here needs a path declared and
nothing needs a symlink — which matters, since a committed symlink does not survive a
`git checkout` on Windows without `core.symlinks=true`.

A local marketplace has to be registered once per machine before the plugin resolves:

```
/plugin marketplace add .
```

Run that from the repository root the first time you open it. Afterwards the skill loads on its
own, as `/hexcore:hexcore` (`plugin-name:skill-name`). If you would rather skip the plugin
entirely, `cp -r skills/hexcore .claude/skills/hexcore` also works and needs no registration —
at the cost of a second copy to keep in sync.

## Using the skill outside this repository

Copy the directory into the project that consumes HexCore:

```bash
cp -r skills/hexcore /path/to/your-project/.claude/skills/hexcore
```

The scripts need only the standard library and do **not** import `hexcore`, so they work on a
bare install with zero extras. Run them with the interpreter of the environment HexCore is
installed in.

## The tools, standalone

They are useful outside an agent session. From the repository root:

```bash
# What is installed, and whether the skill's prose covers it
uv run python skills/hexcore/scripts/hexcore_surface.py --version

# Where a symbol lives -- searches the Python package and the TypeScript client
uv run python skills/hexcore/scripts/hexcore_surface.py --find SqlAlchemyRepository
uv run python skills/hexcore/scripts/hexcore_surface.py --find twoFactor

# Everything one facade exports, and everything the client exports
uv run python skills/hexcore/scripts/hexcore_surface.py --facade cqrs
uv run python skills/hexcore/scripts/hexcore_surface.py --darwin-client

# Verify every hexcore import and every @hexcore-js/darwin-client import resolves
uv run python skills/hexcore/scripts/hexcore_surface.py --check skills/hexcore/ docs/

# Audit a consumer project: removed API, plus the silent failure modes
uv run python skills/hexcore/scripts/hexcore_audit.py src/ --fail-on high
```

`--check` and the audit are worth wiring into a consumer project's own CI. The single most
valuable finding is `alembic-framework-models`: an `env.py` missing
`ensure_framework_models_loaded()` produces a migration that generates cleanly and drops a
table with data in it when applied.

## The gate

`packages/hexcore/tests/test_documentation_examples.py` walks `skills/**/*.md` exactly as it
walks `docs/`, so every `from hexcore… import …` in this directory resolves against the real
package, and every `import { … } from "@hexcore-js/darwin-client"` resolves against the real
client. A rename in either package turns the CI red here.

That gate is the reason this directory exists in the monorepo at all. The skill's previous
life, as a standalone repository with nothing contrasting it against the framework, spent
seven majors teaching `SQLAlchemyCommonImplementationsRepo` — deleted in 7.0.

`scripts/` is outside the walk: it is the tool, not the teaching, and its docstrings carry
deliberately broken examples to explain what it detects.

## Keeping it current

When `hexcore` releases a major, the factual half updates itself — the scripts read the new
package. What needs a human pass is the prose.

1. `--version` will report that the skill's documented major no longer matches. Bump
   `DOCUMENTED_MAJOR` in `scripts/hexcore_surface.py`.
2. `--check skills/hexcore/` names every symbol that was renamed or removed, with its file and
   line. This is also what CI runs.
3. `--deprecated` gives the new deprecation table for `references/removed-api.md`.
4. Add the major's silent behaviour changes to `references/removed-api.md` and
   `references/failure-modes.md` by hand. Those are semantics, not names, and no tool derives
   them.

`@hexcore-js/darwin-client` is on `0.x` and has no documented major: its surface is read from
`packages/darwin-client/src/`, so a rename there surfaces in step 2 as well.

## Language

This directory follows the `docs/` convention, **not** the Spanish-only rule that `CLAUDE.md`
applies to code, docstrings, tests and commit messages: the prose is in English, like
`docs/*/en/`, because that is the reference version and what the skill's audience reads.
Commit messages about this directory are still Spanish, and still conventional — use
`docs(skill):` or `chore(skill):`, never `feat(skill):`, which would bump and publish the
Python package for a change that does not touch it.
