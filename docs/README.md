# HexCore — Documentation

<p align="center">
  <b>A reusable core for Python applications built on hexagonal architecture, DDD, CQRS
  and background tasks.</b>
</p>

---

## Choose your language · Elegí tu idioma

| | |
| :-- | :-- |
| 🇬🇧 **[English documentation](./en/)** | The reference version. Written first. |
| 🇪🇸 **[Documentación en español](./es/)** | Traducción completa, mantenida en sincronía con el original en inglés. |

---

## Other documents

| Document | Language | Covers |
| :-- | :-- | :-- |
| [ARCHITECTURE_TYPING.md](./ARCHITECTURE_TYPING.md) | ES | The type system, the generated stubs and the CI gate. The contract for contributing. |
| [../CHANGELOG.md](../CHANGELOG.md) | ES | Change history. |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | ES | How to contribute. |
| [../SECURITY.md](../SECURITY.md) | ES | Security policy. |

---

## The rule of this documentation

**If a document shows code, there is a test that runs it.**

`tests/test_documentation_examples.py` executes the startup examples, verifies that every
`from hexcore… import …` in these documents resolves against the real API, and that any
attribute asked of a facade is in its `__all__`. The example plugin from the extension guide
lives exercised in `tests/test_darwin_custom_plugin.py`.

The walk is automatic: the test lists `docs/**/*.md`, so a new guide enters the gate without
anyone adding it to a list — which is exactly the file that would otherwise drift without anyone
noticing.

A guide nobody runs ages silently: the day an extension point changes signature, the document
keeps saying the old thing, and the first to find out is someone who has already written half a
plugin following it.

What does **not** live here is the story of how each thing was built. That is in the git
history, which is where history goes.
