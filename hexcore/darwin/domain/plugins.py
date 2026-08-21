"""
El contrato de un plugin de Darwin, y el de sus hooks.

**Cada aporte de un plugin se compone con un mecanismo que HexCore ya tiene.** Es la regla de
diseño del módulo, y la razón de que `DarwinPlugin` no traiga ningún concepto nuevo:

===============================  ==========================================================
Aporta                           Se compone con
===============================  ==========================================================
`tables()`                       Mixins que **el consumidor** declara sobre `Base`
`routers()`                      `MountableRouter` + `mount_routers`
`hooks()`                        La cadena `AbstractMiddleware`, vía `HookMiddleware`
`middlewares()`                  `MiddlewarePipeline` de CQRS
`http_middlewares()`             `BaseHTTPMiddleware` de Starlette
`startup_steps()`                `StartupStep` + `build_lifespan`
`commands()` / `queries()`       `HandlerRegistry`
===============================  ==========================================================

Lo que se **rechaza** de Better Auth, y el motivo en cada caso — todos cambian un error en el
archivo del consumidor por una sorpresa en runtime dentro del framework:

- **Schema contribuido por el plugin.** Un plugin que registra sus propias tablas en
  `Base.metadata` sin que el `env.py` del consumidor las importe hace que
  `alembic revision --autogenerate` emita `op.drop_table`. Por eso `tables()` devuelve
  **mixins**, y la clase concreta la declara el consumidor en su paquete ``models/``.
- **`$Infer`.** No tiene equivalente en Python; se reemplaza por modelos pydantic de
  respuesta en los routers.
- **Lifecycle propio del plugin.** Sería un cuarto mecanismo de extensión al lado de
  `AbstractMiddleware`, `StartupStep` y `DomainEvent`, y nadie sabría cuál usar.
- **Hooks que mutan un `ctx`.** Los hooks de acá son **funciones puras** que devuelven un
  reemplazo. `ValidationMiddleware` ya sienta el precedente de pasarle a `next_handler` una
  instancia reconstruida.
- **Discovery por entry-points.** El registro es explícito: un plugin que se activa por estar
  instalado es un plugin que nadie puede desactivar.

Módulo de dominio puro: stdlib + pydantic. Sin sqlalchemy, sin Starlette, sin crypto.
"""
from __future__ import annotations

import abc
import fnmatch
import typing as t

__all__ = [
    "HookPhase",
    "ShortCircuit",
    "HookBinding",
    "DarwinPlugin",
    "identity_action",
    "action_of",
    "hook_matches",
]

#: Cuándo corre un hook respecto del handler.
#:
#: Sólo dos, y no un `around`: un `around` recibiría `next_handler` y sería un
#: `AbstractMiddleware` con otro nombre — que es lo que `middlewares()` ya expone para quien
#: necesite ese control.
HookPhase = t.Literal["before", "after"]


class ShortCircuit(Exception):  # noqa: N818 - no es un error: es control de flujo
    """
    Corta la cadena y devuelve un resultado, sin ejecutar el handler.

    **No es un error**, y por eso no se llama `...Error`: es el mecanismo con el que un plugin
    responde por su cuenta. Un plugin de 2FA que detecta que falta el segundo factor
    cortocircuita con "hace falta 2FA" en vez de dejar que el sign-in siga.

    En `before` saltea el handler **y los `before` que quedaban**: los hooks siguientes
    esperaban un payload que ya no se va a procesar, y correrlos sería trabajo sobre una
    decisión ya tomada. En `after` reemplaza el resultado.

    Cualquier otra excepción **propaga** — el plugin falla cerrando. Tragarlas dejaría que un
    hook de autorización que explota se lea como un hook que autorizó.
    """

    def __init__(self, result: t.Any = None) -> None:
        super().__init__("El plugin cortocircuitó la cadena.")
        self.result = result


class HookBinding:
    """
    Un hook: a qué acciones se engancha, en qué fase y con qué prioridad.

    `action` acepta comodines de `fnmatch` (`"session.*"`, `"*"`). Los hooks **específicos
    corren antes que los de comodín**, y la razón es práctica: un hook de auditoría con `"*"`
    quiere ver el payload final, no el que llegó antes de que los hooks específicos lo
    ajustaran.

    El callable es `async (payload) -> payload | None`. Devolver `None` significa "no cambio
    nada" y es lo que hace la mayoría; devolver un valor lo **reemplaza**. Nunca se muta el
    argumento: los mensajes de HexCore son `frozen`, así que mutarlos no compila, y eso es
    deliberado.

    Uso::

        HookBinding(
            action="user.sign_in",
            phase="before",
            handler=exigir_segundo_factor,
            priority=10,
        )
    """

    __slots__ = ("action", "phase", "handler", "priority", "plugin")

    def __init__(
        self,
        *,
        action: str,
        phase: HookPhase,
        handler: t.Callable[[t.Any], t.Awaitable[t.Any]],
        priority: int = 100,
        plugin: str = "",
    ) -> None:
        self.action = action
        self.phase = phase
        self.handler = handler
        self.priority = priority
        #: Lo completa el registro. Va en los mensajes de error para poder nombrar al culpable.
        self.plugin = plugin

    @property
    def is_wildcard(self) -> bool:
        return any(c in self.action for c in "*?[")

    def __repr__(self) -> str:  # pragma: no cover - diagnóstico
        return (
            f"HookBinding({self.action!r}, {self.phase!r}, "
            f"priority={self.priority}, plugin={self.plugin!r})"
        )


class DarwinPlugin(abc.ABC):
    """
    Un plugin de Darwin.

    Sólo `name` es obligatorio. Todo lo demás son métodos **concretos** que devuelven vacío,
    así que un plugin declara nada más lo que aporta — y agregar un punto de extensión nuevo
    en una versión futura no rompe a ningún plugin existente. Con métodos abstractos, cada
    plugin tendría que implementar ocho cosas para aportar una.

    Uso::

        class MagicLink(DarwinPlugin):
            name = "magic_link"

            def routers(self):
                return [build_magic_link_router()]

            def tables(self):
                return {"MagicLinkMixin": MagicLinkMixin}
    """

    #: Identificador único. Aparece en los errores del registro y en `requires`.
    name: t.ClassVar[str]

    #: Nombres de otros plugins que tienen que estar registrados **y** ordenados antes.
    #: El registro valida que existan y que no formen ciclo.
    requires: t.ClassVar[tuple[str, ...]] = ()

    #: Desempate del orden entre plugins sin relación de dependencia. Menor corre primero.
    priority: t.ClassVar[int] = 100

    def tables(self) -> t.Mapping[str, type]:
        """
        Los **mixins** que el plugin aporta, por nombre.

        Mixins y no clases mapeadas: ver el docstring del módulo. El consumidor los compone
        con `Base` en su paquete ``models/``, que es lo que hace que `import_all_models` los
        vea y que `--autogenerate` no los dropee.
        """
        return {}

    def hooks(self) -> t.Sequence[HookBinding]:
        """Los hooks que el plugin engancha a acciones de identidad."""
        return ()

    def middlewares(self) -> t.Sequence[t.Any]:
        """Middlewares de CQRS (`AbstractMiddleware`) para el pipeline."""
        return ()

    def http_middlewares(self) -> t.Sequence[tuple[type, t.Mapping[str, t.Any]]]:
        """
        Middlewares HTTP, como `(clase, kwargs)`.

        Tuplas y no instancias porque Starlette los instancia él, pasándole la app.
        """
        return ()

    def routers(self) -> t.Sequence[t.Any]:
        """Routers a montar (`APIRouter`, o `(APIRouter, kwargs)`)."""
        return ()

    def startup_steps(self) -> t.Sequence[t.Any]:
        """Pasos de arranque (`StartupStep`) para `build_lifespan`."""
        return ()

    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        """
        Las excepciones del plugin y su status HTTP.

        Las excepciones viven en el plugin y no en `domain/exceptions.py`: el núcleo no tiene
        por qué conocer los modos de falla de `two_factor`. `create_app` mergea este mapa
        debajo del de identidad, que a su vez va debajo del del consumidor.

        Sin este punto de extensión, la excepción de un plugin saldría como un 500 con el
        traceback — o el consumidor tendría que mapearla a mano, que es pedirle que sepa los
        internos del plugin.
        """
        return {}

    def register_handlers(self, registry: t.Any) -> None:
        """
        Registra los comandos y queries del plugin en el `HandlerRegistry`.

        Recibe el registry en vez de devolver un mapa: `register_command_handler` distingue
        instancias de factories, y devolver un dict obligaría a reimplementar esa distinción.
        """
        return None


# ── Acciones ──────────────────────────────────────────────────────────────────
#: El atributo donde `identity_action` estampa el nombre de la acción.
ACTION_ATTR = "__darwin_action__"


#: El decorador devuelve **la misma clase**, tipada.
#:
#: Sin el TypeVar, `-> type` borra el tipo del comando decorado: pyright pierde los campos y
#: todo `command.email` de un handler pasa a ser desconocido. Un decorador que degrada lo que
#: decora no es aceptable en una API pública.
_TClase = t.TypeVar("_TClase", bound=type)


def identity_action(name: str) -> t.Callable[[_TClase], _TClase]:
    """
    Nombra la acción de un comando o query, para que los hooks puedan engancharse.

    Sin esto el nombre se **deriva** de la clase (`SignIn` → `sign_in`), que alcanza para lo
    que shippea Darwin pero ata el hook al nombre de la clase: renombrar el comando rompería
    en silencio los hooks de todos los plugins. Nombrar la acción explícitamente la convierte
    en el contrato público que es.

    Uso::

        @identity_action("user.sign_in")
        class SignIn(Command):
            ...
    """

    def decorador(clase: _TClase) -> _TClase:
        setattr(clase, ACTION_ATTR, name)
        return clase

    return decorador


def action_of(message: t.Any) -> str:
    """
    El nombre de acción de un mensaje.

    El declarado con `@identity_action` si lo tiene; si no, el de la clase pasado a
    snake_case (`SignOutEverywhere` → `sign_out_everywhere`).
    """
    tipo: type = message if isinstance(message, type) else type(message)
    declarado = getattr(tipo, ACTION_ATTR, None)
    if isinstance(declarado, str):
        return declarado
    return _snake(tipo.__name__)


def _snake(nombre: str) -> str:
    partes: list[str] = []
    for i, char in enumerate(nombre):
        if char.isupper() and i and not nombre[i - 1].isupper():
            partes.append("_")
        partes.append(char.lower())
    return "".join(partes)


def hook_matches(binding: HookBinding, action: str) -> bool:
    """Si el hook aplica a la acción. Comodines de `fnmatch`."""
    return fnmatch.fnmatchcase(action, binding.action)
