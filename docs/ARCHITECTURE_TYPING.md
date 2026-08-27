# Tipado Estricto y Stubs — HexCore

> Documento técnico de arquitectura. Estrategia definitiva de `.pyi`, la regla de la casa
> para `TYPE_CHECKING`, y el blueprint de CI/CD del gate de tipado.
>
> Estado: **diseño aprobado, Fase 0 implementada** (gate midiendo, baseline congelado).
>
> **Fuera de alcance: generación de OpenAPI.** Se maneja aparte, como utilidad específica de
> FastAPI.

---

## 1. El punto de partida, medido

No estimado: medido con `pyright --outputjson` en `strict` sobre `--extra all`.

```
filesAnalyzed: 104    errorCount: 216    warningCount: 3    timeInSec: 12.67
```

**216 errores, 30 archivos con deuda de 104.** Y la distribución es el hallazgo que ordena
todo el plan:

| archivo | errores | % del total |
| :-- | --: | --: |
| `hexcore/infrastructure/repositories/implementations.py` | 64 | 29 % |
| `hexcore/infrastructure/task_queues/procrastinate_adapter.py` | 34 | 15 % |
| `hexcore/infrastructure/task_queues/celery_adapter.py` | 28 | 12 % |
| `hexcore/infrastructure/repositories/utils.py` | 14 | 6 % |
| `hexcore/infrastructure/cqrs/postgres_bus.py` | 12 | 5 % |
| `hexcore/infrastructure/api/health.py` | 8 | 3 % |
| resto (24 archivos) | 56 | 26 % |

**Tres archivos concentran 126 de 216 errores (58 %),** y los tres son exactamente dos de los
siete idiomas de import opcional: el idioma B (clase falsa en `except ImportError`) en
`implementations.py`, y el idioma E (`Celery = t.Any` dentro de `TYPE_CHECKING`) en los dos
adaptadores de colas.

Por regla, los errores dominantes son de la familia "unknown":

| regla | n |
| :-- | --: |
| `reportUnknownMemberType` | 64 |
| `reportUnknownArgumentType` | 36 |
| `reportUnknownVariableType` | 35 |
| `reportUnknownParameterType` | 9 |
| `reportUnusedFunction` | 8 |
| `reportInvalidTypeForm` | 8 |

Eso es la firma de `Any` propagándose: no son errores de lógica, son tipos que se perdieron y
contaminaron todo lo que tocaron. Es consistente con el diagnóstico: los idiomas B y E
destruyen tipos en la raíz y el resto es consecuencia.

**Estado del tooling antes de la Fase 0:** `pyright>=1.1.405` estaba en las dependencias de
dev y **ningún workflow lo corría**. `.vscode/settings.json` tenía
`"python.analysis.typeCheckingMode": "strict"`, así que el editor mostraba los 216 errores y
CI reportaba verde. 49 `# type: ignore` sin código de regla, o sea imposibles de auditar.

---

## 2. Los siete idiomas, y por qué son uno

Hoy conviven **siete** formas de guardar un import opcional. La regla de la casa las
reemplaza por **una**.

| | Idioma | Dónde | Problema |
| :-- | :-- | :-- | :-- |
| **A** | `try: import / except ImportError:` + **clase falsa en el except** | `repositories/base.py:1-13`, `types.py:1-9`, `repositories/utils.py:14-28` | Declaración obscurecida: Pyright resuelve al stub vacío. `types.py` usa `t.Generic[t.TypeVar("M")]` inline, que es inválido. `utils.py` tiene `bound=t.Union[..., t.Any]`, y el `t.Any` **degenera todo el bound a `Any`**. |
| **B** | `try:` envolviendo **el cuerpo entero de la clase** | `implementations.py:43-57,139-141` y `:144-158,214-216` | **El bug #1 de DX, 64 errores.** Pyright ve dos declaraciones y resuelve a `class SqlAlchemyRepository(t.Generic[T, M]): ...` — **sin `save`, sin `get_by_id`, sin `model_cls`, sin `query_cursor`**. |
| **C** | `except ImportError: pass` + `except NameError` alrededor de la clase | `uow/__init__.py:1-11, 37-43, 122-123` | El `except NameError` es **código muerto** (`from __future__ import annotations` hace que las anotaciones no se evalúen). El fallo real es un `isinstance(model, BaseModel)` sin guardar en `uow/__init__.py:99` → `NameError` en runtime. |
| **D** ✅ | `if t.TYPE_CHECKING:` + import normal | `redis_bus.py:18-20`, `postgres_bus.py:17-19`, `rabbitmq.py:17-19`, `uow/scopes.py:22-25`, … | **Correcto.** Único defecto: dos grafías conviven (`if t.TYPE_CHECKING:` vs `from typing import TYPE_CHECKING`). |
| **E** | `if t.TYPE_CHECKING: try: ... except ImportError: X = t.Any` | `celery_adapter.py:109-115`, `procrastinate_adapter.py:16-22` | **62 errores.** Inútil: Pyright evalúa `TYPE_CHECKING` como `True` y analiza **las dos ramas**, así que `Celery` queda `type[Any]` y las firmas son `Any` **incluso con el extra instalado**. |
| **F** | `_ensure_x()` con `ImportError` amable | **sólo** `cqrs/procrastinate.py:20-28` | Es el único de los 8 adaptadores que dice `pip install hexcore[procrastinate]`. Los otros 7 fallan con el `ModuleNotFoundError` crudo de upstream. |
| **G** | Sondas `_x_available()` | `api/health.py:161-207` | `_sql_available` / `_mongo_available` / `_redis_configured`: tres formas y dos convenciones de nombre. Sin registro central, cero constantes `HAS_*`. |

Más un octavo: `implementations.py:103-122` hace los imports **dentro del cuerpo del método**
con un forward-ref en string.

---

## 3. La regla de la casa

### 3.1 Grafía única

```python
from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
```

- **`import typing as t` + `if t.TYPE_CHECKING:`**, nunca `from typing import TYPE_CHECKING`.
  Es lo que ya usa la mayoría del árbol; se unifica el resto.
- El bloque contiene **sólo imports**. Ni `try`, ni asignaciones, ni `X = t.Any`.
- Va inmediatamente después de los imports de runtime.

### 3.2 Todo módulo es "hoja con extra" o "núcleo"

La distinción que faltaba y que hace la regla verificable. Se declara en `pyproject.toml`, así
que CI la puede chequear:

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

- **Hoja con extra**: importa su extra en el top level, libremente. Su docstring lo dice.
  Sólo se alcanza a través de una fachada perezosa.
- **Núcleo**: **nunca** importa un extra en el top level. Sólo `if t.TYPE_CHECKING:` y
  lazy-imports dentro de funciones.

⚠️ Hoy esa frontera la sostienen **dos archivos de 0 bytes sin documentar**:
`cache/cache_backends/__init__.py` e `infrastructure/__init__.py`. Si alguien pone un
re-export en el primero, `hexcore/__init__.py:20` (`from .infrastructure import cache`)
empieza a arrastrar `redis`. El job `no-extras-import` lo convierte en un invariante verificado.

### 3.3 El reemplazo del idioma B: **reestructurar, no stubbear**

El caso difícil, y el de mayor retorno (64 errores). `SqlAlchemyRepository` tiene que
conservar sus completions **y** `hexcore.infrastructure.repositories.implementations` tiene
que seguir importable sin los extras, **y** `tests/test_optional_dependencies.py` tiene que
seguir pasando.

**Un `.pyi` no sirve acá.** Sería una segunda copia de una clase de ~100 líneas que hay que
mantener a mano: la definición de deuda de mantenimiento.

**Solución: la clase guardada se muda a una hoja con extra, y el módulo núcleo la
re-exporta perezosamente.**

Antes (`implementations.py`, 64 errores):

```python
try:
    from .orms.sqlalchemy import BaseModel
    M = t.TypeVar("M", bound=BaseModel[t.Any])

    class SqlAlchemyRepository(BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]):
        @property
        def model_cls(self) -> t.Type[M]: ...
        async def save(self, entity: T) -> T: ...
        # ... ~100 líneas
except ImportError:
    M = t.TypeVar("M")                                  # type: ignore
    class SqlAlchemyRepository(t.Generic[T, M]): ...    # type: ignore  ← Pyright resuelve ACÁ
```

Después:

```python
# hexcore/infrastructure/repositories/orms/sqlalchemy/repository.py
#   HOJA CON EXTRA [sql]: importa sqlalchemy en el top level, sin guardas.
#   Una sola definición, sin ramas, sin `# type: ignore`.
from .base import BaseSQLAlchemyRepository
from . import BaseModel

M = t.TypeVar("M", bound=BaseModel[t.Any])

class SqlAlchemyRepository(BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]):
    ...
```

```python
# hexcore/infrastructure/repositories/implementations.py
#   NÚCLEO: importable sin ningún extra.

if t.TYPE_CHECKING:
    # El checker lee las definiciones REALES. Una sola declaración, sin obscurecer.
    from .orms.sqlalchemy.repository import SqlAlchemyRepository as SqlAlchemyRepository
    from .orms.beanie.repository import BeanieRepository as BeanieRepository
else:
    # En runtime, PEP 562: se resuelve al primer acceso y falla con un mensaje útil, no con
    # un `ModuleNotFoundError` crudo ni con una clase vacía.
    __getattr__ = lazy_attrs(__name__, {
        "SqlAlchemyRepository": (".orms.sqlalchemy.repository", "sql"),
        "BeanieRepository": (".orms.beanie.repository", "mongo"),
    })
```

Por qué el `else` y no sólo el `if`: si `__getattr__` se define en las dos ramas, Pyright ve
un módulo con `__getattr__` y **deja de reportar atributos inexistentes** en él — se pierde
la detección de typos. Con `else`, el checker ve un namespace cerrado y explícito.

**Los cuatro contratos que se preservan, y cómo se prueban:**

| Contrato | Prueba |
| :-- | :-- |
| `import implementations` funciona sin `sql`/`mongo` | `tests/test_optional_dependencies.py`, ya existe |
| `SqlAlchemyRepository` tiene todos sus métodos para Pyright | `tests/typing/` con `assert_type`; falla antes, pasa después |
| `hexcore.sql.SqlAlchemyRepository` sigue resolviendo | `tests/test_facades.py`, sin cambios |
| Los alias deprecados siguen andando | `tests/test_deprecations.py`, sin cambios |

### 3.4 La muerte del idioma E (62 errores)

```python
# ANTES: inútil. Pyright analiza las dos ramas, `Celery` queda `type[Any]`, y
# `def __init__(self, app: "Celery")` es `Any` incluso con celery instalado.
if t.TYPE_CHECKING:
    try:
        from celery import Celery
    except ImportError:
        Celery = t.Any

# DESPUÉS: el tipo es real cuando el extra está; el error amable es de runtime.
if t.TYPE_CHECKING:
    from celery import Celery

class CeleryTaskEnqueuer:
    def __init__(self, app: "Celery") -> None:
        require_extra("celery", "CeleryTaskEnqueuer")   # ImportError con el pip install
        self._app = app
```

### 3.5 Un solo error amable, para los 8 extras

Generaliza el `_ensure_procrastinate()` que hoy es el único que ayuda:

```python
# hexcore/_extras.py
_PAQUETE_POR_EXTRA = {
    "sql": "sqlalchemy", "api": "fastapi", "redis": "redis", "mongo": "beanie",
    "rabbitmq": "aio_pika", "procrastinate": "procrastinate", "celery": "celery",
    "darwin": "joserfc",
}


def require_extra(extra: str, consumidor: str) -> None:
    """
    Verifica que el extra esté instalado, o lanza un `ImportError` con la remediación.

    Los 8 adaptadores usan éste. Antes, 7 de 8 fallaban con el `ModuleNotFoundError` crudo
    de upstream, que no dice qué extra instalar ni por qué hacía falta.
    """
    modulo = _PAQUETE_POR_EXTRA[extra]
    if importlib.util.find_spec(modulo) is None:
        raise ImportError(
            f"{consumidor} necesita el extra [{extra}], que no está instalado.\n\n"
            f"    pip install hexcore[{extra}]\n"
            f"    # o: uv add 'hexcore[{extra}]'\n"
        )
```

Y un único registro de capacidades (`hexcore/capabilities.py`) con constantes `HAS_SQL`,
`HAS_API`, … reemplazando las tres sondas de `health.py`.

### 3.6 Política de `# type: ignore`

Orden estricto, y **cada paso después** de que el anterior esté limpio:

1. Narrow: `# type: ignore` → `# pyright: ignore[reglaConcreta]` + razón en castellano.
2. `reportUnnecessaryTypeIgnoreComment = "error"` — CI dice cuáles ya no hacen falta.
3. `reportMissingTypeStubs = "error"` (necesita el grupo `typecheck` con los stubs de
   croniter/asyncpg/celery/pika).
4. `enableTypeIgnoreComments = false`, **sólo** con conteo provablemente cero. Prenderlo con
   uno pendiente des-suprime en silencio lo que estaba tapando.

---

## 4. Estrategia `.pyi`

### 4.1 Stubs para exactamente tres módulos

Un `.pyi` es una segunda copia que hay que mantener sincronizada: es deuda. Se justifica
**sólo** cuando ningún checker puede evaluar la superficie del módulo.

**Regla: inline por default. Un `.pyi` sólo cuando la superficie es computada en runtime.**

Eso deja exactamente tres: `hexcore/cqrs.py` (54 entradas), `hexcore/fastapi.py` (41) y
`hexcore/sql.py` (24). **119 símbolos.**

### 4.2 El problema que resuelven

Las tres fachadas canónicas — las que la documentación publicita como "un import obvio por
tarea" — hacen:

```python
_EXPORTS: dict[str, tuple[str, str]] = {
    "Base": ("hexcore.infrastructure.repositories.orms.sqlalchemy", "Base"),
    ...
}

__all__ = sorted(_EXPORTS)          # ← expresión de RUNTIME: Pyright no la puede evaluar

def __getattr__(name: str) -> t.Any:  # ← TODO export tipa `Any`
    ...
```

**Los 119 exports de la superficie pública recomendada tipan `Any`.**

Y la ironía: la superficie **deprecada** sí tiene shims que la hacen resoluble
(`domain/cqrs/__init__.py:100-104`, `buses.py:90-94`, `handlers.py:60-62`, `serializer.py:45-46`,
`middleware.py:57-58`, `uow/__init__.py:217-218`, `events.py:102-103`). Lo viejo tipa bien y
lo nuevo tipa `Any`.

### 4.3 Ejemplo trabajado: `hexcore/sql.py` + `hexcore/sql.pyi`

El fuente (sin cambios, sigue siendo perezoso):

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
    globals()[name] = value          # se cachea: el segundo acceso no pasa por acá
    return value
```

El stub, **generado**:

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

**Cómo resuelve Pyright el par, y por qué el stub no rompe la pereza.** Ante `sql.py` y
`sql.pyi`, Pyright usa **exclusivamente el `.pyi`** y no mira el `.py`. En runtime, Python
usa **exclusivamente el `.py`** y no mira el `.pyi`. No compiten: el stub describe la
superficie estática, el fuente implementa el comportamiento dinámico. `import hexcore.sql`
sigue sin arrastrar sqlalchemy.

Detalles no obvios:

- **`as X` redundante a propósito.** En un `.pyi`, un import sin `as` no se considera
  re-exportado (PEP 484). Sin el `X as X`, el stub no exporta nada.
- **`__all__` literal**, que es lo que `sorted(_EXPORTS)` no puede ser para un checker.
  Ordenado igual que en runtime, y hay un test que lo compara.
- **Sin `def __getattr__`.** Declararlo haría que Pyright acepte cualquier atributo y se
  pierde la detección de typos. La excepción es `implementations.py`, que **sí** necesita
  declararlo por los alias deprecados.

### 4.4 Generación y gate de drift

Un stub generado que se desincroniza es peor que no tenerlo: promete símbolos que no existen.

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

Se eligió un generador propio sobre las alternativas:

| Herramienta | Veredicto |
| :-- | :-- |
| `stubgen` (mypy) | **No.** Importa o parsea el módulo y emite `def __getattr__(name: str) -> Any`, que es exactamente el problema. |
| `pyright --createstub` | **No.** Pensado para librerías de terceros sin tipos; genera el esqueleto completo, no la re-exportación. |
| **Propio, desde `_EXPORTS`** | ✅ `_EXPORTS` **es** la fuente de verdad. 119 entradas, mapeo mecánico, determinista. |

El `--check` falla con remediación copiable, en el estilo de la casa:

```
::error::hexcore/sql.pyi está desincronizado con el _EXPORTS de hexcore/sql.py.
Regeneralo:

    uv run python scripts/gen_stubs.py --write
```

### 4.5 Tests de tipo: hoy hay **cero**

Cero ocurrencias de `assert_type`, `reveal_type`, `pyright` o `py.typed` bajo `tests/`. El
diseño:

```python
# tests/typing/test_facades_no_tipan_any.py
#   No se ejecuta: se le pasa Pyright. Marker `typing`, deseleccionado por default.
from typing import assert_type

from hexcore.sql import Base, SqlAlchemyUnitOfWork


def test_las_fachadas_exponen_tipos_reales() -> None:
    # Falla ANTES de los stubs (todo era `Any`) y pasa después. Es la demostración
    # ejecutable de que el problema #1 está muerto.
    assert_type(Base, type[Base])
    assert_type(SqlAlchemyUnitOfWork, type[SqlAlchemyUnitOfWork])


def test_el_repositorio_generico_conserva_sus_parametros() -> None:
    from hexcore.sql import SqlAlchemyRepository

    class MiRepo(SqlAlchemyRepository[MiEntidad, MiModelo]):
        ...

    # Antes resolvía al stub vacío del `except ImportError` y esto era un error.
    reveal_type(MiRepo().save)
```

Más **tests negativos**: un `# pyright: ignore[...]` invertido que fija los errores que
**tienen** que seguir apareciendo, para que apagar una regla no pase inadvertido.

---

## 5. Blueprint de CI/CD

### 5.1 Workflow nuevo, no jobs dentro de `pytest.yml`

Se eligió `typing.yml` aparte:

- `pytest.yml` tiene un invariante propio y fuerte ("un solo SKIPPED falla el job") que no
  aplica al tipado. Mezclarlos hace que un fallo de tipado se lea como un fallo de tests.
- El gate tiene que ser invocable por `workflow_call` desde `publish-to-pypi.yml`.
- Los nombres de job de una matriz son dinámicos y no se pueden marcar como required checks;
  hace falta un agregador de nombre fijo, y ése es un concepto del gate, no de los tests.

Se copian los triggers y el filtro de rama base de `pytest.yml`, incluido el detalle de
`release/**` y `feat/**` para las cadenas de PRs apilados.

### 5.2 Los jobs, y en qué fase entra cada uno

| Job | Qué hace | Fase |
| :-- | :-- | :-- |
| **`typecheck`** | pyright strict + anotaciones `::error file=,line=::` + **ratchet** | **0 ✅** |
| **`typing-ok`** | Agregador de nombre fijo para branch protection | **0 ✅** |
| `packaging` | Wheel con `py.typed` + los `.pyi`; `twine check` | **0 ✅** (en `pytest.yml`) |
| `stubs-drift` | Regenera y difea. El más rápido: no importa hexcore ni necesita extras | 3 |
| `stub-quality` | `--verifytypes` con umbral de completeness que sólo sube | 3 |
| `no-extras-import` | Instala **sin extras** y asegura que todo módulo núcleo importa | 5 |
| `extras-matrix` | 9 patas: `none` + cada extra + `all` | 5 |
| `house-rules` | Las reglas de §3 verificadas mecánicamente | 6 |

**El orden importa y es la lección del propio repo.** Cada job entra **después** de que el
código que gatea esté limpio. Al revés, el primer PR pone master en rojo, y un gate que
arranca rojo se desactiva — que es exactamente cómo se llegó a tener pyright en las
dependencias de dev y en ningún workflow.

### 5.3 El ratchet, y por qué no "cero errores"

Exigir cero con 216 de deuda es un gate que se apaga en el segundo PR. Exigir "no peor que
ayer" se puede prender **hoy**.

`typing-baseline.json` guarda presupuesto **por archivo** más el total. Reglas:

1. Un archivo que se pasa de su presupuesto → **falla**.
2. Un archivo **nuevo** con cualquier error → **falla**. Ésta es la cláusula que impide que la
   deuda crezca: la vieja se tolera, la nueva no.
3. Un archivo que mejora → `::notice::` para que alguien baje el baseline. **No falla.**
4. **`--update` nunca corre en CI.** El baseline se mueve en un PR humano, con diff y revisor,
   así que `git blame` sigue sirviendo para las regresiones de tipado.

Verificado que los dos modos de regresión disparan:

```
$ python scripts/typing_ratchet.py errors --report pyright.json     # baseline manipulado
::error::hexcore/infrastructure/api/health.py: 8 error(es), el baseline permite 2.
exit=1

$ python scripts/typing_ratchet.py errors --report pyright.json     # archivo sacado del baseline
::error::hexcore/infrastructure/api/health.py: 8 error(es) y no está en el baseline.
          Un archivo nuevo arranca en cero — la deuda vieja se tolera, la nueva no.
exit=1
```

La completeness usa el mismo mecanismo con una **banda muerta de 0.5 pp**, para que el ruido
de coma flotante y el drift entre parches de pyright no generen avisos espurios de "mejoraste".

`PYRIGHT_VERSION` se pinea: una actualización de pyright que agregue reglas es su propio PR,
cuyo diff es `typing-baseline.json` más los arreglos — nunca una sorpresa dentro del PR de
otra persona.

### 5.4 `--verifytypes` estaba bloqueado por el empaquetado

`--verifytypes` resuelve el paquete por PEP 561, así que necesita el `py.typed` en
`site-packages`. Sin `[build-system]`, el paquete no se instalaba y devolvía
`No py.typed file found` con completeness 0. **La Fase 0 es el desbloqueo de este job.**

Y se instala la **wheel**, no un editable: los editables de setuptools usan un
`MetaPathFinder` propio que Pyright no sigue. Instalar la wheel es determinista y de paso
verifica que el `py.typed` llegó al artefacto que se publica.

### 5.5 La matriz de extras cierra el punto ciego

Hoy CI corre **un solo** `uv sync --extra all`, que esconde exactamente una clase de bug: un
módulo del extra `[redis]` que importa algo de `[sql]` funciona con `all` y explota para quien
instaló sólo `hexcore[redis]`.

9 patas: `none`, `api`, `redis`, `mongo`, `sql`, `rabbitmq`, `procrastinate`, `celery`, `all`.
Verifica **importabilidad, no comportamiento**: correr pytest con un extra suelto haría saltar
20 `importorskip` y el invariante de "cero SKIPPED" de `pytest.yml` no se toca.

⚠️ Se introduce con `continue-on-error: true` por **exactamente un PR**. La matriz va a
encontrar fugas reales la primera vez que corra —es su propósito—, y una matriz que arranca en
rojo se borra.

### 5.6 Enganche con `publish-to-pypi`

**Sí, publicar debe depender del gate**, y por una razón concreta y no general: lo único que
hace que el tipado llegue al usuario es el `package-data` de la wheel, que es una propiedad
**de build time**. Un master verde no prueba nada sobre el artefacto si el artefacto lo
construye otro workflow con otro toolchain (`pip` + `python -m build`, contra `uv` en todo el
resto).

```yaml
jobs:
  gate:
    name: "Gate de tipado"
    uses: ./.github/workflows/typing.yml    # una sola definición, la misma que en los PRs
  build:
    needs: gate
```

Se descartó consultar la API de Actions (`gh api`) para ver si el run del gate sobre este SHA
quedó verde: necesita `permissions: actions: read`, tiene una carrera con el orden de disparo
de los dos workflows, y falla de forma confusa cuando el run todavía está corriendo.

**`bump_ver.yml` no se toca.** Poner el gate antes del bump significaría que un master rojo
bloquea los bumps de versión — lo que suena bien pero no lo es: master ya está rojo en ese
punto y bloquear el bump sólo agrega un segundo fallo que investigar.

**Branch protection: exactamente dos checks requeridos** — `Python tests / test` y
`Gate de tipado`. Todo lo demás llega por `needs`.

---

## 6. Orden de migración

Cada fase termina con master en verde.

| Fase | Qué | Riesgo |
| :-- | :-- | :-- |
| **T0 ✅** | `[tool.pyright]` strict sin reglas nuevas; ratchet; baseline de 216; `typing.yml` con `typecheck` + `typing-ok`; `ruff` a dev | **Ninguno.** El gate pasa por definición el día uno. |
| **T1 ✅** | `[build-system]`, `package-data`, `packages.find`, borrar el `__init__.py` raíz, `tests/test_packaging.py` | Medio → **verificado.** `uv sync` ahora instala el proyecto; se confirmó que `hexcore.__file__` sigue apuntando al repo. |
| **T2** | `hexcore/_lazy.py`; los `.pyi` generados de las 3 fachadas; `tests/typing/`; jobs `stubs-drift` + `stub-quality` | Bajo. Un `.pyi` no puede romper runtime. |
| **T3** | `hexcore/capabilities.py` + `_extras.require_extra`; reemplazar las 3 sondas; declarar `starlette` en `[api]` y `pymongo` en `[mongo]` | Bajo. |
| **T4** | Los 7 idiomas, **de menor a mayor radio**: E (62 err) → A en `types.py`/`base.py`/`utils.py` (~15) → C en `uow` → **B en `implementations.py` (64 err), el último** | B es el riesgoso: crea 2 archivos, borra ~180 líneas y cambia lo que resuelve `hexcore.sql.SqlAlchemyRepository`. Va último, contra un baseline ya limpio, con los 4 contratos de §3.3 verificados. |
| **T5** | `test_optional_dependencies.py` derivado de la matriz; jobs `no-extras-import` + `extras-matrix` | Medio: va a encontrar fugas reales. `continue-on-error` por un PR. |
| **T6** | Narrow de los `# type: ignore`; prender las 4 reglas **en orden** (§3.6); `house-rules` | Bajo si se respeta el orden. |
| **T7** | Unificar los 3 mecanismos de deprecación; PEP 702 `@deprecated`; `reportDeprecated = "error"` | Bajo. |
| **T8** | Enganchar el gate a `publish-to-pypi`; branch protection | La primera release después de esto conviene que sea un tag `rc` deliberado. |

**Lo que NO se hace en v6:** eliminar los alias anteriores a 5.0. `REMOVED_IN` ya se movió a
`7.0` en la Fase 0. Quien actualizó a 6.0.0 lo hizo con los alias presentes y funcionando;
sacárselos en un parche sería romper la promesa al revés. Se eliminan en un 7.0 de verdad.

---

## 7. Resumen

Siete idiomas de import opcional colapsan en **uno**: `import typing as t` + un único
`if t.TYPE_CHECKING:` con nada más que imports, y todo módulo clasificado —en un manifiesto
verificado por CI— como **hoja con extra** o **núcleo**. Las clases envueltas en `try` y los
stubs falsos del `except ImportError` no se stubbean: se **reestructuran**, mudando la clase
guardada a una hoja y re-exportándola con PEP 562 detrás de un `if not t.TYPE_CHECKING`, para
que el checker lea la definición real y el namespace quede cerrado.

Existen `.pyi` para **exactamente tres módulos** —las fachadas manejadas por `_EXPORTS`, cuya
superficie ningún checker puede evaluar— y se **generan desde `_EXPORTS` con un gate de
drift**, así que no son deuda de mantenimiento.

`require_extra()` le da un error amable a los 8 adaptadores; `hexcore.capabilities` da un solo
juego de `HAS_*`; y las cuatro reglas de supresión se prenden en orden, cada una después de
que su código esté limpio.

Un `typing.yml` reusable corre pyright-strict detrás de un **ratchet por archivo** sembrado
con los **216 errores medidos**, `--verifytypes` detrás de un umbral de completeness que la
Fase 0 acaba de desbloquear, una **matriz de 9 patas** que termina con el punto ciego de
`--extra all`, y un job de empaquetado que prueba que la wheel efectivamente lleva `py.typed`
— con el workflow de publicación invocando el mismo gate, para que una wheel sin tipos no
pueda llegar a PyPI.
