# Escribir un plugin de Darwin

Darwin trae seis plugins, pero el sistema de plugins **no es para ellos**: es la superficie por
la que tu aplicación mete su propia lógica de identidad sin tocar el núcleo ni hacer un fork.

Un plugin puede aportar rutas HTTP, comandos y queries de CQRS, middlewares, pasos de arranque,
tablas y —lo que más se usa— **hooks** que se enganchan a los flujos que Darwin ya tiene.

> Todo el código de esta guía vive ejecutado en
> [`tests/test_darwin_custom_plugin.py`](../../tests/test_darwin_custom_plugin.py). Si esta
> guía miente, esos tests se ponen rojos. Si editás una, editá la otra.

---

## Lo mínimo que es un plugin

Un `name` y nada más:

```python
from hexcore.darwin.domain.plugins import DarwinPlugin


class MiPlugin(DarwinPlugin):
    name = "mi_plugin"
```

Eso ya se registra y valida. **Todos los puntos de extensión son métodos concretos que
devuelven vacío**, así que declarás sólo lo que aportás. La razón no es comodidad: con métodos
abstractos, cada plugin tendría que implementar ocho cosas para aportar una, y agregar un punto
de extensión nuevo en una versión futura rompería a todos los plugins existentes.

`name` es el identificador: aparece en los errores del registro, es lo que otros plugins ponen
en `requires`, y es el nombre del paquete que `ensure_identity_schema_loaded(plugins=[...])`
espera si aportás tablas.

---

## Los puntos de extensión

| Método | Qué aporta | Cuándo lo llama Darwin |
| :-- | :-- | :-- |
| `hooks()` | `HookBinding`s sobre acciones de identidad | En cada acción, por `run_hooks` |
| `routers()` | `APIRouter`, o `(APIRouter, kwargs)` | Al montar, vía `plugins.routers()` |
| `register_handlers(registry)` | Comandos y queries en el `HandlerRegistry` | Al construir el CQRS |
| `exception_status_map()` | `{ExcepciónDelPlugin: status}` | `create_app` lo mergea |
| `middlewares()` | `AbstractMiddleware` del pipeline CQRS | Al construir el pipeline |
| `http_middlewares()` | `(clase, kwargs)` para Starlette | Al construir la app |
| `startup_steps()` | `StartupStep` para `build_lifespan` | Al arrancar |
| `tables()` | Mixins de SQLAlchemy, por nombre | **Nunca**: lo llama el consumidor |

Y tres atributos de clase:

| Atributo | Default | Para qué |
| :-- | :-- | :-- |
| `name` | — obligatorio | Identificador |
| `requires` | `()` | Otros plugins que tienen que estar **y correr antes** |
| `priority` | `100` | Desempate entre plugins sin relación. Menor corre primero |
| `contributed_tables` | `()` | Los nombres de los mixins de `tables()`, **sin importarlos** |

---

## Hooks: el punto de extensión que vas a usar

Un hook se engancha a una **acción** en una **fase**:

```python
from hexcore.darwin.domain.plugins import DarwinPlugin, HookBinding


class HorarioPermitidoPlugin(DarwinPlugin):
    """Rechaza el sign-in fuera de una ventana horaria."""

    name = "horario_permitido"
    priority = 10

    def __init__(self, *, desde: int = 8, hasta: int = 20) -> None:
        self._desde = desde
        self._hasta = hasta

    def hooks(self):
        return [
            HookBinding(
                action="user.sign_in",
                phase="before",
                handler=self._verificar_horario,
                priority=10,
            ),
        ]

    async def _verificar_horario(self, payload):
        hora = getattr(payload, "hora", None)
        if hora is not None and not (self._desde <= hora < self._hasta):
            raise FueraDeHorarioError(
                f"El acceso está permitido de {self._desde} a {self._hasta}."
            )
        return None
```

### El contrato del handler

Es `async (payload) -> payload | None`.

**Devolver `None` significa "no cambio nada"**, y es lo que hace la mayoría —los hooks que sólo
observan—. Devolver un valor lo **reemplaza** para el hook siguiente y para el handler. Se
encadena, no se acumula: así un hook puede refinar lo que hizo el anterior.

Nunca mutes el argumento. Los mensajes de HexCore son `frozen`, así que mutarlos no compila, y
eso es deliberado.

### Comodines y orden

`action` acepta comodines de `fnmatch`: `"session.*"`, `"*"`. Los **hooks específicos corren
antes que los de comodín**, y la razón es práctica: un hook de auditoría con `"*"` quiere ver el
payload final, no el que llegó antes de que los específicos lo ajustaran.

Dentro de cada grupo manda `priority`: menor corre primero.

---

## La trampa: tu excepción tiene que ser un `IdentityError`

Esto es lo que más cuesta descubrir solo, así que va con nombre y apellido.

`run_hooks` trata tres casos distinto:

- **`ShortCircuit`** propaga tal cual. No es un error: es el mecanismo con el que un plugin
  responde por su cuenta.
- **`IdentityError`** propaga tal cual, porque es una señal deliberada del dominio.
- **Cualquier otra excepción** se envuelve en un `RuntimeError` que nombra al plugin, la fase y
  la acción.

O sea: si tu excepción hereda de `Exception` a secas, el framework la trata como **un plugin
roto** y el consumidor recibe un 500 con un mensaje que habla de tu plugin fallando — no de tu
regla de negocio rechazando.

```python
from hexcore.darwin.domain.exceptions import IdentityError


class FueraDeHorarioError(IdentityError):   # ← IdentityError, no Exception
    """El plugin corta el sign-in fuera de la ventana permitida."""
```

Y para que salga con el status correcto en vez de un 500, declarala:

```python
    def exception_status_map(self):
        return {FueraDeHorarioError: 403}
```

Las excepciones viven en el plugin y no en `domain/exceptions.py`: el núcleo no tiene por qué
conocer los modos de falla de tu plugin. `create_app` mergea este mapa debajo del de identidad,
que a su vez va debajo del del consumidor — así que podés sobreescribirlo desde tu app.

Que las otras excepciones se envuelvan **no es hostilidad**: es que el plugin falla cerrando.
Tragarlas dejaría que un hook de autorización que explota se lea como un hook que autorizó.

---

## Cortocircuitar: responder sin que corra el handler

```python
from hexcore.darwin.domain.plugins import DarwinPlugin, HookBinding, ShortCircuit


class CachePlugin(DarwinPlugin):
    name = "cache"

    def __init__(self, respuesta):
        self._respuesta = respuesta

    def hooks(self):
        return [
            HookBinding(
                action="user.sign_in",
                phase="before",
                handler=self._responder,
                priority=1,
            )
        ]

    async def _responder(self, payload):
        raise ShortCircuit(self._respuesta)
```

En `before` saltea el handler **y los `before` que quedaban**: los hooks siguientes esperaban un
payload que ya no se va a procesar. En `after` reemplaza el resultado.

Es el mecanismo con el que un plugin de 2FA corta un sign-in con "hace falta segundo factor" en
vez de dejar que siga.

---

## Nombres de acción

Por defecto la acción se **deriva** del nombre de la clase del mensaje:
`SignOutEverywhere` → `sign_out_everywhere`.

Eso alcanza para lo que shippea Darwin, pero ata el hook al nombre de la clase: renombrar el
comando rompería en silencio los hooks de todos los plugins. Si escribís comandos propios a los
que otros se van a enganchar, nombrá la acción explícitamente:

```python
from hexcore.darwin.domain.plugins import identity_action


@identity_action("user.sign_in")
class SignIn(Command):
    ...
```

El decorador devuelve **la misma clase, tipada** — no degrada lo que decora.

---

## Orden entre plugins

`requires` declara dependencias y el registro las valida **al cablear**, no en el primer
request:

```python
class Encima(DarwinPlugin):
    name = "encima"
    requires = ("base",)
```

Cuatro cosas se rechazan ahí mismo, y cada error nombra al culpable:

1. **Nombre duplicado.** Quedarse con uno en silencio haría que qué plugin corre dependa del
   orden de importación.
2. **`requires` que no existe.** Correría igual, sin lo que necesita, y fallaría más adelante en
   un lugar que no señala la causa.
3. **Ciclo de dependencias.** Acá es un error con los nombres del ciclo; en producción sería un
   `RecursionError` o un orden arbitrario.
4. **Conflicto de tablas.** Dos plugins que aportan un mixin homónimo.

El orden es **topológico** por `requires`, con `(priority, orden de registro)` de desempate. Es
determinista a propósito: si dependiera del hash de un set, el mismo cableado daría cadenas de
hooks distintas entre corridas.

**`requires` gana sobre `priority`.** Un plugin con `priority = 1` que requiere a otro con
`priority = 900` corre igual después.

---

## Rutas, comandos y el resto

```python
    def routers(self):
        # Import diferido: mantiene barato importar el plugin, y no exige `[api]`
        # hasta que alguien pida el router.
        from mi_paquete.router import build_router

        return [build_router()]

    def register_handlers(self, registry):
        from mi_paquete.commands import MiComando, MiComandoHandler

        registry.register_command_handler(
            MiComando, registry.factory(MiComandoHandler)
        )
```

`register_handlers` recibe el registry en vez de devolver un mapa porque
`register_command_handler` distingue instancias de factories, y devolver un dict obligaría a
reimplementar esa distinción.

---

## Si tu plugin guarda cosas

Darwin no impone un backend, y **tu plugin tampoco debería**. El núcleo resuelve el
almacenamiento por **contrato de nombre neutro**: cada backend expone los mismos nombres, y
quien los junta nunca nombra un backend.

Estructura esperada:

```
mi_plugin/
  __init__.py          el DarwinPlugin
  domain.py            los puertos (Abstract*) y las entidades
  orms/
    sqlalchemy/
      models.py        los mixins + PLUGIN_MODELS
      repository.py    la implementación del puerto
    beanie/
      repository.py    los documentos + PLUGIN_DOCUMENTS
```

Dos constantes con **nombre fijo** son las que hacen que el esquema llegue a Alembic y a
`init_beanie`:

- `PLUGIN_MODELS` en `orms/sqlalchemy/models.py`
- `PLUGIN_DOCUMENTS` en `orms/beanie/repository.py`

Sin ellas tu tabla existe en la base y **está ausente de `Base.metadata`**, y el próximo
`alembic revision --autogenerate` le emite `op.drop_table`. Con datos adentro. Es el peor modo
de falla del módulo porque es el único que no da error.

Si tu plugin implementa un solo backend, está bien: quien lo cablee con el otro recibe un
`ImportError` al arrancar que dice cuáles implementás. Pero **no lo fuerces desde el
`pyproject`**: "uno de dos" no se expresa en metadata de empaquetado, y declarar los dos le
instalaría SQLAlchemy a quien eligió Mongo.

### `tables()` y `contributed_tables`

`tables()` devuelve **mixins**, no clases mapeadas. El consumidor los compone con su `Base` en
su paquete `models/`, que es lo que hace que `import_all_models` los vea.

`contributed_tables` es la misma lista de nombres **sin importar nada**. Es una duplicación
deliberada, y existe por un motivo concreto: el registro necesita los nombres para detectar el
conflicto de dos plugins con un mixin homónimo, y llamar a `tables()` para eso importaría
SQLAlchemy — o sea que un despliegue en Mongo no podría ni registrar tu plugin.

```python
class MiPlugin(DarwinPlugin):
    name = "mi_plugin"
    contributed_tables = ("MiMixin",)

    def tables(self):
        from mi_paquete.orms.sqlalchemy.models import MiMixin

        return {"MiMixin": MiMixin}
```

Declararlo es opcional —sin declaración el registro cae a `tables()`—, pero es lo que hace que
tu plugin sirva en Mongo.

---

## Cablearlo

```python
from hexcore.darwin import IdentityConfig, PluginRegistry, configure_identity

configure_identity(
    IdentityConfig(),
    plugins=[HorarioPermitidoPlugin(desde=8, hasta=20)],
)
```

`plugins=` acepta una lista o una tupla de instancias, o un `PluginRegistry` ya armado. Una
clase sin instanciar, un generador o un `set` se rechazan con un `TypeError` que dice por qué:
un generador se consume una sola vez y un `set` no tiene orden, y el orden de los plugins es el
que decide el orden de los hooks.

Y para montar sus rutas:

```python
from hexcore.darwin import build_identity_router, get_identity_container

plugins = get_identity_container().plugins

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    routers=[build_identity_router(), *plugins.routers()],
)
```

---

## Empaquetarlo aparte

Si tu plugin es una distribución propia, seguí la convención de los extras de Darwin: hacé que
tu extra **arrastre `hexcore[darwin]`**.

```toml
[project.optional-dependencies]
mi-plugin = ["hexcore[darwin]", "lo-que-necesites"]
```

Sin esa autorreferencia, `pip install 'mi-paquete[mi-plugin]'` instala un plugin sin núcleo, y
el import se rompe.

---

## Checklist

- [ ] `name` declarado, único y estable — es contrato público.
- [ ] Las excepciones que emitís a propósito heredan de `IdentityError`.
- [ ] Están en `exception_status_map()` con su status.
- [ ] Los hooks son `async` y devuelven `None` si no cambian nada.
- [ ] No mutás el payload.
- [ ] Los imports pesados van **diferidos**, adentro del método.
- [ ] Si aportás tablas: `PLUGIN_MODELS` / `PLUGIN_DOCUMENTS`, y `contributed_tables` declarado.
- [ ] Si tenés `requires`, no formás ciclo.
- [ ] Tu extra arrastra `hexcore[darwin]`.

---

## Ver también

- [Los seis plugins que vienen incluidos](./plugins-incluidos.md)
- [Almacenamiento, esquema y Alembic](./almacenamiento.md)
- [`tests/test_darwin_custom_plugin.py`](../../tests/test_darwin_custom_plugin.py) — todo esto,
  ejecutado
