"""
Remoción, en 7.0, de la superficie de API anterior a 5.0.

Los alias de v1/v2 (`ICommandBus`, `ISerializer`, `NoSqlUnitOfWork`, …) estaban deprecados
desde 5.0 y **se eliminaron en 7.0**: dos majors completos de aviso.

Este módulo se invirtió en vez de borrarse, y la diferencia importa. Los tests siguen
enumerando los 20 nombres, pero ahora fijan tres cosas distintas:

1. El alias **ya no resuelve** — y falla como `AttributeError`, que es lo que Python promete
   para un nombre inexistente, no como algo raro.
2. El **canónico sigue existiendo** y no avisa. Si un alias se hubiera removido borrando de
   más, esto lo agarra.
3. El mecanismo de deprecación **sigue en pie** para lo que venga: la constante de versión
   apunta al futuro, y hay un test que falla si vuelve a quedar en el pasado.

Ese tercer punto es el que convierte "se nos pasó otra vez" en "no se puede releasear".
"""
from __future__ import annotations

import importlib
import tomllib
import warnings

import pytest

from hexcore._deprecation import REMOVED_IN
from rutas import PAQUETE as REPO_ROOT


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _published_version() -> str:
    """
    La versión que declara el repo.

    Se lee de `pyproject.toml` y no de `importlib.metadata.version("hexcore")`: los
    metadatos del dist instalado se quedan en la versión del último `pip install`, así
    que en un checkout de desarrollo mienten. El que tiene que ir por delante del bump
    es el aviso, y el bump vive aquí.
    """
    return tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]


def _release(version: str, width: int = 3) -> tuple[int, ...]:
    """
    `"7.0"` → `(7, 0, 0)`. Sólo el tramo numérico, rellenado a `width` componentes.

    El relleno es lo que hace la comparación honesta: sin él `("7","0") > ("6","0","0")`
    compara tuplas de distinta longitud y `"6.0"` vs `"6.0.0"` daría "menor" en vez de
    "igual", que es justo el caso contradictorio que hay que detectar.
    """
    parts: list[int] = []
    for chunk in version.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple((parts + [0] * width)[:width])


def test_removed_in_is_ahead_of_the_published_version():
    """
    El aviso promete una eliminación **futura**, así que `REMOVED_IN` tiene que ser
    estrictamente mayor que la versión publicada.

    Sin esta comprobación, un `feat!:` involuntario deja el aviso diciendo "se eliminará
    en 6.0" mientras corre en 6.0.0 con los alias todavía presentes: el usuario no sabe
    cuánto margen tiene y aprende que estos avisos son basura. En este repo ya hubo dos
    majors accidentales, así que no es hipotético.
    """
    published = _release(_published_version())
    removal = _release(REMOVED_IN)

    assert removal > published, (
        f"REMOVED_IN es {REMOVED_IN!r} pero el paquete ya publica "
        f"{_published_version()!r}: el aviso anuncia una eliminación que, según él "
        f"mismo, ya debería haber ocurrido. Subí REMOVED_IN al próximo major."
    )


# (módulo, alias removido, nombre canónico que lo reemplaza)
ALIASES_REMOVIDOS = [
    ("hexcore.domain.cqrs.buses", "ICommandBus", "AbstractCommandBus"),
    ("hexcore.domain.cqrs.buses", "IQueryBus", "AbstractQueryBus"),
    ("hexcore.domain.cqrs.buses", "IEventBus", "AbstractEventBus"),
    ("hexcore.domain.cqrs.handlers", "ICommandHandler", "AbstractCommandHandler"),
    ("hexcore.domain.cqrs.handlers", "IQueryHandler", "AbstractQueryHandler"),
    ("hexcore.domain.cqrs.middleware", "IMiddleware", "AbstractMiddleware"),
    ("hexcore.domain.cqrs.serializer", "ISerializer", "AbstractSerializer"),
    ("hexcore.domain.cqrs", "ICommandBus", "AbstractCommandBus"),
    ("hexcore.domain.cqrs", "IQueryBus", "AbstractQueryBus"),
    ("hexcore.domain.cqrs", "IEventBus", "AbstractEventBus"),
    ("hexcore.domain.cqrs", "ICommandHandler", "AbstractCommandHandler"),
    ("hexcore.domain.cqrs", "IQueryHandler", "AbstractQueryHandler"),
    ("hexcore.domain.cqrs", "IMiddleware", "AbstractMiddleware"),
    ("hexcore.domain.cqrs", "ISerializer", "AbstractSerializer"),
]

# Los que dependen de un extra: mismo trato, pero el módulo necesita `[sql]`/`[mongo]`.
ALIASES_REMOVIDOS_OPCIONALES = [
    (
        "hexcore.infrastructure.repositories.implementations",
        "SQLAlchemyCommonImplementationsRepo",
        "SqlAlchemyRepository",
    ),
    (
        "hexcore.infrastructure.repositories.implementations",
        "BeanieODMCommonImplementationsRepo",
        "BeanieRepository",
    ),
    ("hexcore.infrastructure.uow", "NoSqlUnitOfWork", "BeanieUnitOfWork"),
]

TODOS = ALIASES_REMOVIDOS + ALIASES_REMOVIDOS_OPCIONALES


#: Nombres removidos cuyo reemplazo **no vive en el mismo módulo**, así que no entran en la
#: parametrización de `TODOS` —que busca el canónico dentro de `module_path`— y se verifican
#: acá, con el módulo del reemplazo explícito.
#:
#: `IEventDispatcher` se fue en 7.0 y `EventBus`, que era su reemplazo, se fue en 11.0: hoy el
#: canónico de los dos es `AbstractEventBus`. Así termina una deprecación encadenada —el nombre
#: intermedio también se elimina— y por eso la lista guarda el par final y no el histórico.
REMOVIDOS_CON_CANONICO_EN_OTRO_MODULO = [
    (
        "hexcore.domain.events",
        "IEventDispatcher",
        "hexcore.domain.cqrs.buses",
        "AbstractEventBus",
    ),
    (
        "hexcore.domain.events",
        "EventBus",
        "hexcore.domain.cqrs.buses",
        "AbstractEventBus",
    ),
    ("hexcore", "PermissionsRegistry", "hexcore.darwin", "RoleRegistry"),
    ("hexcore", "TokenClaims", "hexcore.darwin", "AccessTokenClaims"),
]

#: Módulos que se eliminaron **enteros** en 11.0.
#:
#: Se verifican aparte de los nombres porque el modo de falla es otro: un módulo que se fue no
#: da `AttributeError` al pedirle un nombre, da `ModuleNotFoundError` al importarlo, y quien
#: tenía `from hexcore.domain.auth import TokenClaims` arriba de un archivo lo descubre en el
#: import y no en el uso.
MODULOS_REMOVIDOS = [
    "hexcore.domain.auth",
    "hexcore.domain.auth.permissions",
    "hexcore.domain.auth.value_objects",
    "hexcore.infrastructure.events",
    "hexcore.infrastructure.events.events_backends",
    "hexcore.infrastructure.events.events_backends.memory",
]


# ── 1. El alias ya no resuelve ─────────────────────────────────────────────────
@pytest.mark.parametrize(("module_path", "alias", "_canonical"), TODOS)
def test_el_alias_removido_no_resuelve(module_path, alias, _canonical):
    modulo = importlib.import_module(module_path)

    with pytest.raises(AttributeError):
        getattr(modulo, alias)


@pytest.mark.parametrize(("module_path", "alias", "_canonical"), TODOS)
def test_el_alias_removido_no_esta_en_all(module_path, alias, _canonical):
    """
    Anunciar en `__all__` algo que no existe rompe `from modulo import *` con un
    `AttributeError` que no menciona el `__all__` — un error muy difícil de rastrear.
    """
    modulo = importlib.import_module(module_path)

    assert alias not in getattr(modulo, "__all__", [])


@pytest.mark.parametrize(("module_path", "alias", "_canonical"), TODOS)
def test_el_error_del_alias_removido_es_el_estandar_de_python(
    module_path, alias, _canonical
):
    """
    Tiene que decir "has no attribute", como cualquier nombre inexistente. Un mensaje a medida
    que hable de deprecación sugeriría que el nombre existe en algún modo, y no existe.
    """
    modulo = importlib.import_module(module_path)

    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(modulo, alias)


# ── 2. El canónico sigue, y no avisa ───────────────────────────────────────────
@pytest.mark.parametrize(("module_path", "_alias", "canonical"), TODOS)
def test_el_canonico_sigue_existiendo(module_path, _alias, canonical):
    """Si la remoción se llevó de más, esto lo agarra."""
    modulo = importlib.import_module(module_path)

    assert getattr(modulo, canonical) is not None


@pytest.mark.parametrize(("module_path", "_alias", "canonical"), TODOS)
def test_el_canonico_no_avisa(module_path, _alias, canonical):
    """
    Pedir el nombre bueno no puede emitir nada.

    Hasta 11.0 esta parametrización tenía que filtrar dos casos —`EventBus` y el
    `InMemoryEventBus` de `infrastructure.events` eran el reemplazo de un alias de 5.0 y a la
    vez estaban deprecados ellos mismos—, así que el canónico de esas filas sí avisaba. Con
    los dos eliminados, la cadena terminó y el filtro sobra: hoy no queda ningún canónico
    deprecado, y si mañana vuelve a haber uno, este test lo dice.
    """
    modulo = importlib.import_module(module_path)

    with warnings.catch_warnings(record=True) as capturados:
        warnings.simplefilter("always")
        getattr(modulo, canonical)

    deprecaciones = [
        w for w in capturados if issubclass(w.category, DeprecationWarning)
    ]
    assert deprecaciones == []


# ── Lo que 11.0 eliminó, en la fecha que cada aviso anunció ───────────────────
@pytest.mark.parametrize(
    ("module_path", "nombre", "_modulo_canonico", "_canonico"),
    REMOVIDOS_CON_CANONICO_EN_OTRO_MODULO,
)
def test_el_nombre_removido_no_resuelve(
    module_path, nombre, _modulo_canonico, _canonico
):
    """
    Ya no resuelve **ni avisando**: el `__getattr__` que emitía el aviso se fue con el nombre.

    Es la diferencia entre deprecar y remover, y es la que hace honesto al aviso: mientras el
    nombre avisaba, seguía funcionando; ahora no está, y el error lo dice sin rodeos.
    """
    modulo = importlib.import_module(module_path)

    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(modulo, nombre)


@pytest.mark.parametrize(
    ("module_path", "nombre", "_modulo_canonico", "_canonico"),
    REMOVIDOS_CON_CANONICO_EN_OTRO_MODULO,
)
def test_el_nombre_removido_no_esta_en_all(
    module_path, nombre, _modulo_canonico, _canonico
):
    modulo = importlib.import_module(module_path)

    assert nombre not in getattr(modulo, "__all__", [])


@pytest.mark.parametrize(
    ("_module_path", "_nombre", "modulo_canonico", "canonico"),
    REMOVIDOS_CON_CANONICO_EN_OTRO_MODULO,
)
def test_el_canonico_del_removido_existe(
    _module_path, _nombre, modulo_canonico, canonico
):
    """
    La otra dirección: si la remoción se llevó de más, o el reemplazo que el aviso prometía
    nunca existió, el usuario se queda sin a dónde migrar y lo descubre después de borrar su
    código viejo.
    """
    modulo = importlib.import_module(modulo_canonico)

    assert getattr(modulo, canonico, None) is not None


@pytest.mark.parametrize("module_path", MODULOS_REMOVIDOS)
def test_el_modulo_removido_no_importa(module_path):
    """El módulo entero se fue, así que el import falla — no queda un shim vacío que resuelva."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_path)


@pytest.mark.parametrize(
    "module_path",
    [
        "hexcore",
        "hexcore.domain.cqrs",
        "hexcore.domain.cqrs.buses",
        "hexcore.domain.cqrs.handlers",
        "hexcore.domain.cqrs.middleware",
        "hexcore.domain.cqrs.serializer",
        "hexcore.domain.events",
    ],
)
def test_importar_no_avisa(module_path):
    """
    Importar HexCore no puede emitir deprecaciones. Si el import avisara, el usuario no
    podría saber **quién** usa un nombre deprecado, y el aviso se volvería ruido a ignorar.
    """
    import subprocess
    import sys

    resultado = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            f"import {module_path}",
        ],
        capture_output=True,
        text=True,
    )

    assert resultado.returncode == 0, resultado.stderr


# ── 3. Los métodos y campos removidos ──────────────────────────────────────────
def test_register_y_dispatch_se_removieron():
    """
    `register()` / `dispatch()` → `subscribe()` / `publish()`.

    Se verifica sobre `AbstractEventBus`, que es el puerto que quedó: el `EventBus` sobre el
    que se escribió este test en 7.0 era un alias suyo, y en 11.0 se eliminó.

    Ojo: `register` **sigue existiendo** pero es el de `ABCMeta` —el registro de subclases
    virtuales de la stdlib—, no el método deprecado. Se distingue por su `__qualname__`, no
    por su presencia.
    """
    from hexcore.domain.cqrs.buses import AbstractEventBus

    assert AbstractEventBus.register.__qualname__ == "ABCMeta.register"
    assert not hasattr(AbstractEventBus, "dispatch")

    assert hasattr(AbstractEventBus, "subscribe")
    assert hasattr(AbstractEventBus, "publish")


def test_reset_sqlalchemy_engine_se_removio():
    pytest.importorskip("sqlalchemy")
    from hexcore.infrastructure.repositories.orms.sqlalchemy import session

    assert not hasattr(session, "reset_sqlalchemy_engine")
    assert "reset_sqlalchemy_engine" not in session.__all__
    assert hasattr(session, "dispose_engine")


def test_server_config_event_dispatcher_se_removio():
    from hexcore.config import ServerConfig

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = ServerConfig(debug=False)

    assert not hasattr(config, "event_dispatcher")
    assert config.event_bus is not None


def test_pasar_event_dispatcher_falla_con_remediacion():
    """
    **El detalle que evita un fallo silencioso.**

    Pydantic ignora los kwargs que no conoce, así que sin un rechazo explícito quien migre
    pasando `event_dispatcher=` se quedaría con el bus por defecto sin enterarse — y el
    síntoma aparecería mucho más tarde como "mis eventos no llegan".
    """
    from hexcore.config import ServerConfig

    with pytest.raises(ValueError) as excinfo:
        ServerConfig(debug=False, event_dispatcher=object())

    mensaje = str(excinfo.value)
    assert "event_bus" in mensaje
    assert "7.0" in mensaje


# ── 4. Un nombre inexistente sigue siendo AttributeError ───────────────────────
@pytest.mark.parametrize(
    "module_path",
    [
        "hexcore.domain.cqrs",
        "hexcore.domain.cqrs.buses",
        "hexcore.domain.events",
        "hexcore.infrastructure.uow",
    ],
)
def test_un_nombre_inexistente_sigue_siendo_attribute_error(module_path):
    """
    Al quitar los `__getattr__` de deprecación, el módulo vuelve al comportamiento por
    defecto. Se verifica que siga siendo `AttributeError` y no algo raro.
    """
    modulo = importlib.import_module(module_path)

    with pytest.raises(AttributeError, match="has no attribute"):
        modulo.EstoNoExiste


# ── 5. El mecanismo sigue en pie para lo que venga ─────────────────────────────
def test_el_mecanismo_de_deprecacion_sigue_disponible():
    """
    Se removió el inventario, no el mecanismo. Darwin lo necesita en Fase 10 para deprecar
    `hexcore.domain.auth`.
    """
    from hexcore import _deprecation

    assert callable(_deprecation.deprecated_aliases)
    assert callable(_deprecation.deprecated_callable)
    assert callable(_deprecation.warn_deprecated)


def test_deprecated_aliases_sigue_funcionando():
    """El helper se ejercita solo, sin depender de que quede algún alias vivo en el árbol."""
    from hexcore._deprecation import deprecated_aliases

    class Canonico:
        pass

    globales = {"Canonico": Canonico}
    getattr_de_modulo = deprecated_aliases(
        "modulo.falso", {"Viejo": "Canonico"}, globales
    )

    with pytest.warns(DeprecationWarning, match="Viejo"):
        assert getattr_de_modulo("Viejo") is Canonico

    with pytest.raises(AttributeError, match="has no attribute"):
        getattr_de_modulo("NoExiste")


def test_la_fecha_de_remocion_sigue_estando_en_el_futuro():
    """
    `REMOVED_IN` tiene que ser mayor que el major publicado.

    Estuvo en "6.0" mientras el paquete ya era 6.0.0, así que cada `DeprecationWarning`
    prometía una remoción que —según el propio aviso— ya había ocurrido, mientras los alias
    seguían ahí. Un aviso que se contradice a sí mismo es peor que no avisar.

    Este test es el que hace que el próximo bump de major no pueda repetirlo en silencio: o
    removés lo deprecado, o corrés la constante, pero no podés releasear con la promesa
    vencida.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

    major_actual = int(version.split(".")[0])
    major_de_remocion = int(REMOVED_IN.split(".")[0])

    assert major_de_remocion > major_actual, (
        f"REMOVED_IN es {REMOVED_IN!r} y la versión publicada es {version}: el aviso "
        f"promete una remoción que ya pasó. O eliminá lo deprecado, o corré REMOVED_IN al "
        f"próximo major ({major_actual + 1}.0)."
    )
