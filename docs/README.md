# HexCore — Documentación

<p align="center">
  <b>Núcleo reutilizable para aplicaciones Python con arquitectura hexagonal, DDD, CQRS
  y tareas en background.</b>
</p>

---

## Elegí tu idioma · Choose your language

| | |
| :-- | :-- |
| 🇪🇸 **[Documentación en español](./es/)** | La versión de referencia. Se escribe primero acá. |
| 🇬🇧 **[English documentation](./en/)** | Full translation, kept in sync with the Spanish source. |

---

## Otros documentos · Other documents

| Documento | Idioma | Qué cubre |
| :-- | :-- | :-- |
| [ARCHITECTURE_TYPING.md](./ARCHITECTURE_TYPING.md) | ES | El sistema de tipos, los stubs generados y el gate de CI. Contrato para contribuir. |
| [../CHANGELOG.md](../CHANGELOG.md) | ES | Historial de cambios. |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | ES | Cómo contribuir. |
| [../SECURITY.md](../SECURITY.md) | ES | Política de seguridad. |

---

## La regla de esta documentación

**Si un documento muestra código, hay un test que lo corre.**

`tests/test_documentation_examples.py` ejecuta los ejemplos de arranque, verifica que cada
`from hexcore… import …` de estos documentos resuelva contra la API real, y que todo atributo
que se le pida a una fachada esté en su `__all__`. El plugin de ejemplo de la guía de extensión
vive ejercitado en `tests/test_darwin_custom_plugin.py`.

El recorrido es automático: el test lista `docs/**/*.md`, así que una guía nueva entra al gate
sin que nadie la agregue a una lista — que es justo el archivo que, si no, se desalinea sin que
nadie se entere.

Una guía que no corre envejece sin avisar: el día que un punto de extensión cambia de firma, el
documento sigue diciendo lo de antes y el primero en enterarse es alguien que ya escribió medio
plugin siguiéndolo.

Lo que **no** vive acá es la historia de cómo se construyó cada cosa. Eso está en el historial
de git, que es donde va la historia.
