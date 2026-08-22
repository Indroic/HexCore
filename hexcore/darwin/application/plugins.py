"""
El registro de plugins: valida al construir y resuelve el orden.

Cuatro cosas se rechazan **al registrar**, no en el primer request, y cada error nombra al
culpable:

1. **Nombre duplicado.** Dos plugins con el mismo `name` sólo pueden ser un cableado
   duplicado, y quedarse con uno en silencio hace que qué plugin corre dependa del orden de
   importación.
2. **`requires` que no existe.** Un plugin que declara depender de otro que nadie registró
   correría igual, sin lo que necesita, y fallaría más adelante en un lugar que no señala la
   causa.
3. **Ciclo de dependencias.** Descubierto acá es un error con los nombres del ciclo;
   descubierto al ordenar en producción sería un `RecursionError` o un orden arbitrario.
4. **Conflicto de tablas.** Dos plugins que aportan un mixin con el mismo nombre le dejarían
   al consumidor la duda de cuál compuso.

El orden es **topológico** por `requires`, con `(priority, orden de registro)` como desempate
entre plugins sin relación. Determinista a propósito: si el orden dependiera del hash de un
set, el mismo cableado daría cadenas de hooks distintas entre corridas.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.domain.plugins import DarwinPlugin, HookBinding, hook_matches

__all__ = ["PluginError", "PluginRegistry"]


class PluginError(ValueError):
    """Un plugin está mal declarado o mal cableado. Se detecta al registrar."""


class PluginRegistry:
    """
    Los plugins de un despliegue, validados y ordenados.

    Uso::

        registro = PluginRegistry()
        registro.register(MagicLinkPlugin())
        registro.register(TwoFactorPlugin())
        registro.validate()          # o dejá que `plugins` lo llame solo

        for plugin in registro.plugins:
            ...
    """

    def __init__(self, plugins: t.Iterable[DarwinPlugin] = ()) -> None:
        self._registrados: list[DarwinPlugin] = []
        self._orden: tuple[DarwinPlugin, ...] | None = None
        #: Cache de los hooks que aplican a cada acción. Ver `hooks_for`.
        self._cache_hooks: dict[tuple[str, str], tuple[HookBinding, ...]] = {}
        for plugin in plugins:
            self.register(plugin)

    # ── Registro ──────────────────────────────────────────────────────────────
    def register(self, plugin: DarwinPlugin) -> "PluginRegistry":
        """
        Registra un plugin. Fluido: devuelve `self`.

        Raises:
            PluginError: si el plugin no declara `name`, o si el nombre ya está.
        """
        nombre = getattr(type(plugin), "name", None)
        if not isinstance(nombre, str) or not nombre:
            raise PluginError(
                f"{type(plugin).__qualname__} no declara `name`. Todo plugin necesita un "
                f"identificador: es lo que otros plugins ponen en `requires` y lo que "
                f"aparece en los errores.\n\n"
                f"    class {type(plugin).__qualname__}(DarwinPlugin):\n"
                f'        name = "mi_plugin"\n'
            )

        ya = self._por_nombre().get(nombre)
        if ya is not None:
            raise PluginError(
                f"Ya hay un plugin llamado '{nombre}' ({type(ya).__qualname__}), y se está "
                f"registrando otro ({type(plugin).__qualname__}). Quedarse con uno en "
                f"silencio haría que cuál corre dependa del orden de importación."
            )

        self._registrados.append(plugin)
        self._invalidar()
        return self

    def _invalidar(self) -> None:
        self._orden = None
        self._cache_hooks.clear()

    def _por_nombre(self) -> dict[str, DarwinPlugin]:
        return {type(p).name: p for p in self._registrados}

    # ── Validación y orden ────────────────────────────────────────────────────
    @property
    def plugins(self) -> tuple[DarwinPlugin, ...]:
        """Los plugins en orden de ejecución. Valida la primera vez, y cachea."""
        if self._orden is None:
            self._orden = self._resolver_orden()
        return self._orden

    def validate(self) -> None:
        """
        Valida el registro. Idempotente.

        Raises:
            PluginError: dependencia faltante, ciclo, o conflicto de tablas.
        """
        self.plugins
        self._validar_tablas()

    def _resolver_orden(self) -> tuple[DarwinPlugin, ...]:
        por_nombre = self._por_nombre()
        indice = {type(p).name: i for i, p in enumerate(self._registrados)}

        # 1. Toda dependencia declarada tiene que existir.
        for plugin in self._registrados:
            for requerido in type(plugin).requires:
                if requerido not in por_nombre:
                    raise PluginError(
                        f"El plugin '{type(plugin).name}' declara `requires = "
                        f"(..., '{requerido}', ...)` y '{requerido}' no está registrado. "
                        f"Registralo antes, o sacá la dependencia.\n\n"
                        f"Registrados: {', '.join(sorted(por_nombre)) or '(ninguno)'}"
                    )

        # 2. Orden topológico con DFS y detección de ciclo. Los hijos se visitan ordenados
        #    por `(priority, orden de registro)`, que es lo que hace el resultado
        #    determinista entre corridas.
        orden: list[DarwinPlugin] = []
        estado: dict[str, str] = {}
        camino: list[str] = []

        def visitar(nombre: str) -> None:
            actual = estado.get(nombre)
            if actual == "listo":
                return
            if actual == "visitando":
                ciclo = camino[camino.index(nombre) :] + [nombre]
                raise PluginError(
                    f"Las dependencias de los plugins forman un ciclo: "
                    f"{' -> '.join(ciclo)}. Un plugin no puede depender de sí mismo, ni "
                    f"directa ni indirectamente."
                )

            estado[nombre] = "visitando"
            camino.append(nombre)
            plugin = por_nombre[nombre]
            for requerido in sorted(
                type(plugin).requires,
                key=lambda n: (getattr(type(por_nombre[n]), "priority", 100), indice[n])
                if n in por_nombre
                else (0, 0),
            ):
                visitar(requerido)
            camino.pop()
            estado[nombre] = "listo"
            orden.append(plugin)

        for nombre in sorted(
            por_nombre, key=lambda n: (type(por_nombre[n]).priority, indice[n])
        ):
            visitar(nombre)

        return tuple(orden)

    def _nombres_de_tablas(self, plugin: DarwinPlugin) -> tuple[str, ...]:
        """
        Los nombres de los mixins de un plugin, sin importar un backend si se puede evitar.

        Prefiere `contributed_tables`, que son los mismos nombres declarados sin imports. La
        validación corre en todo `configure_identity`, así que leerlos de `tables()` hacía que
        registrar `two_factor` en un despliegue de Mongo explotara con un `ImportError` sobre un
        paquete que ese despliegue eligió no instalar.

        Cae a `tables()` cuando el plugin **no** declara, y eso no es indulgencia: un plugin de
        terceros escrito antes de que `contributed_tables` existiera seguiría implementando sólo
        `tables()`, y saltearlo lo dejaría fuera del chequeo de homónimos **en silencio** — que es
        peor que el import, porque el conflicto que el chequeo existe para encontrar volvería a
        aparecer como un error dentro del framework.

        Si ese `tables()` no se puede importar, se saltea. Es la única salida honesta: no se
        pueden leer los nombres de un módulo que no está, y hacer fallar el arranque por no poder
        correr una validación es peor que no correrla.
        """
        declarados = type(plugin).contributed_tables
        if declarados:
            return tuple(declarados)
        try:
            return tuple(plugin.tables())
        except ImportError:
            return ()

    def _validar_tablas(self) -> None:
        """Detecta dos plugins que aportan un mixin homónimo. Ver `_nombres_de_tablas`."""
        vistos: dict[str, str] = {}
        for plugin in self.plugins:
            for nombre_tabla in self._nombres_de_tablas(plugin):
                dueno = vistos.get(nombre_tabla)
                if dueno is not None:
                    raise PluginError(
                        f"Los plugins '{dueno}' y '{type(plugin).name}' aportan los dos un "
                        f"mixin llamado '{nombre_tabla}'. Renombrá uno: si no, el consumidor "
                        f"no puede saber cuál está componiendo."
                    )
                vistos[nombre_tabla] = type(plugin).name

    # ── Agregación de aportes ─────────────────────────────────────────────────
    def table_names(self) -> tuple[str, ...]:
        """
        Los nombres de los mixins aportados, sin importar ningún backend.

        Es lo que se puede preguntar en cualquier despliegue. `tables()` da los objetos, y para
        eso hace falta sqlalchemy.
        """
        self._validar_tablas()
        return tuple(
            nombre
            for plugin in self.plugins
            for nombre in self._nombres_de_tablas(plugin)
        )

    def tables(self) -> dict[str, type]:
        """
        Todos los mixins aportados, en orden de plugin.

        ⚠️ **Requiere `[darwin-sqlalchemy]`**: los mixins son de SQLAlchemy. Lo llama el consumidor
        que está declarando sus modelos concretos, no el framework — ver `DarwinPlugin.tables`.
        Para los nombres solos, `table_names()`.
        """
        self._validar_tablas()
        acumulado: dict[str, type] = {}
        for plugin in self.plugins:
            acumulado.update(plugin.tables())
        return acumulado

    def routers(self) -> list[t.Any]:
        return [r for plugin in self.plugins for r in plugin.routers()]

    def middlewares(self) -> list[t.Any]:
        return [m for plugin in self.plugins for m in plugin.middlewares()]

    def http_middlewares(self) -> list[tuple[type, t.Mapping[str, t.Any]]]:
        return [m for plugin in self.plugins for m in plugin.http_middlewares()]

    def startup_steps(self) -> list[t.Any]:
        return [s for plugin in self.plugins for s in plugin.startup_steps()]

    def exception_status_map(self) -> dict[type[Exception], int]:
        """
        El mapa de excepciones combinado. El del último plugin gana sobre el del primero.

        Que gane el último es lo consistente con el resto: `create_app` mergea el de identidad
        debajo de éste, y el del consumidor arriba de todo.
        """
        acumulado: dict[type[Exception], int] = {}
        for plugin in self.plugins:
            acumulado.update(plugin.exception_status_map())
        return acumulado

    def register_handlers(self, registry: t.Any) -> t.Any:
        for plugin in self.plugins:
            plugin.register_handlers(registry)
        return registry

    def hooks(self) -> list[HookBinding]:
        """
        Todos los hooks, con `plugin` ya completado.

        El nombre del plugin se estampa acá y no lo declara el hook: hacerlo a mano sería un
        dato duplicado que se puede desincronizar, y su único uso es aparecer en los errores.
        """
        acumulado: list[HookBinding] = []
        for plugin in self.plugins:
            for binding in plugin.hooks():
                binding.plugin = type(plugin).name
                acumulado.append(binding)
        return acumulado

    def hooks_for(self, action: str, phase: str) -> tuple[HookBinding, ...]:
        """
        Los hooks que aplican a una acción y fase, en orden de ejecución.

        **Memoizado**, y hace falta: esto corre en cada mensaje, y sin cache cada uno pagaría
        un `fnmatch` por hook registrado. El cache se invalida al registrar un plugin nuevo.

        Orden: los **específicos antes que los de comodín**, y dentro de cada grupo por
        `(priority, orden de registro)`. Un hook de auditoría con `"*"` quiere ver el payload
        final, no el que llegó antes de que los específicos lo ajustaran.
        """
        clave = (action, phase)
        cacheado = self._cache_hooks.get(clave)
        if cacheado is not None:
            return cacheado

        candidatos = [
            (i, b)
            for i, b in enumerate(self.hooks())
            if b.phase == phase and hook_matches(b, action)
        ]
        candidatos.sort(key=lambda par: (par[1].is_wildcard, par[1].priority, par[0]))

        resultado = tuple(b for _, b in candidatos)
        self._cache_hooks[clave] = resultado
        return resultado

    # ── Introspección ─────────────────────────────────────────────────────────
    @property
    def names(self) -> tuple[str, ...]:
        """Los nombres en orden de ejecución."""
        return tuple(type(p).name for p in self.plugins)

    def get(self, name: str) -> DarwinPlugin | None:
        return self._por_nombre().get(name)

    def __len__(self) -> int:
        return len(self._registrados)

    def __bool__(self) -> bool:
        # Explícito: sin esto, un registro vacío es falsy y un `if registro:` lo descartaría.
        # Es el mismo defecto que `InMemoryTaskEnqueuer` documenta en el kit de testing.
        return True
