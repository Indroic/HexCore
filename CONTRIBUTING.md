# Pautas de Colaboración

¡Gracias por tu interés en contribuir a HexCore! Para mantener una colaboración organizada y eficiente, sigue estas pautas:

## 1. Código de Conducta
Por favor, mantén siempre una comunicación respetuosa y profesional. Revisa el [Código de Conducta](CODE_OF_CONDUCT.md) antes de interactuar.

## 2. Cómo Contribuir
- **Forkea** el repositorio y crea una rama para tu contribución (`feature/nombre`, `fix/nombre`, etc.).
- Realiza tus cambios en la rama y asegúrate de que el código funcione correctamente.
- Escribe una descripción clara y detallada en tu pull request (PR).
- Relaciona los issues relevantes en tu PR si aplica.

## 3. Estilo y Formato de Código
- Sigue la guía de estilos de Python ([PEP8](https://pep8.org/)).
- Usa comentarios cuando sea necesario para clarificar el propósito del código.
- Idealmente, incluye pruebas unitarias para nuevas funciones y arreglos.

## 4. El `CHANGELOG.md` no se toca en los PRs

**Regla: ningún PR edita `CHANGELOG.md`.** Lo genera `commitizen` en el release, a partir de
los commits convencionales. Si un cambio merece prosa, esa prosa va en el **cuerpo del
commit**, no en el changelog.

No es una preferencia de estilo. Un changelog se escribe por *prepend*, así que cada rama y
cada `cz bump` en master editan **la misma línea 1**: con N ramas abiertas, cada release
rompe las N. En la ronda de mejoras anterior costó tres tandas de conflictos, y el coste
subió cada vez — la primera fue borrar tres marcadores, la tercera exigió reubicar bloques
entre secciones.

Como los commits ya son convencionales y `commitizen` ya está configurado
(`[tool.commitizen]` en `pyproject.toml`), el changelog sale solo:

```sh
cz bump            # calcula la versión, taggea y regenera el CHANGELOG
```

Lo que sí se te pide a cambio: **escribí el commit para que se lea en el changelog**. El
asunto es la línea que va a aparecer publicada; el cuerpo explica el *porqué*, que es lo que
un `git log` no puede reconstruir después.

### Poner al día una rama divergida: `merge`, no `rebase`

```sh
git config rerere.enabled true     # una vez, antes de empezar
git merge origin/master
```

Con `rebase`, un conflicto se repite una vez por cada commit que tocó el archivo. Con
`rerere` activado, una resolución se reaplica sola en el resto de las ramas de la pila.

### "No puedo mergear" casi nunca es un conflicto

`mergeable: MERGEABLE` sólo dice que no hay conflictos; lo que bloquea el botón es
`mergeStateStatus`. Diagnosticá antes de tocar git:

```sh
gh pr view <n> --json mergeable,mergeStateStatus,reviewDecision
gh pr checks <n>
```

`DIRTY` = conflictos reales · `UNSTABLE` = checks pendientes o fallando · `BEHIND` = la base
exige estar al día · `BLOCKED` = falta review · `CLEAN` = listo.

## 5. Revisión de Pull Requests
- Todos los PR serán revisados antes de ser aceptados. Se pueden solicitar cambios o aclaraciones.
- Responde a los comentarios de los revisores para facilitar el proceso.

## 6. Issues
- Describe claramente los problemas que encuentres.
- Proporciona información relevante (logs, versiones, pasos para reproducir, etc.).

## 7. Comunicación
- Usa los issues y las discusiones para preguntas, sugerencias o propuestas.
- Si tienes dudas sobre cómo empezar, puedes abrir un issue para orientación.

## 8. Licencia
Al contribuir, aceptas que tu código será distribuido bajo la licencia del repositorio.

---

¡Gracias por colaborar!
