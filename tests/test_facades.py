"""
S3: módulos fachada `hexcore.cqrs`, `hexcore.sql` y `hexcore.fastapi`.

Requisitos:
- Reexportan lo público **sin mover nada de sitio**: la ruta larga sigue valiendo y
  devuelve exactamente el mismo objeto.
- Nada de I/O ni side effects en import time (S5), y por tanto el import de la fachada no
  arrastra las dependencias opcionales.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


FACADES = ["hexcore.cqrs", "hexcore.sql", "hexcore.fastapi"]


@pytest.mark.parametrize("facade_name", FACADES)
def test_facade_exports_resolve_to_the_canonical_object(facade_name):
    """La fachada no crea objetos nuevos: apunta a los de siempre."""
    facade = importlib.import_module(facade_name)

    for name, (module_path, attribute) in facade._EXPORTS.items():
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            pytest.skip(f"{module_path} necesita un extra no instalado")
        expected = getattr(module, attribute)
        assert getattr(facade, name) is expected, f"{facade_name}.{name}"


@pytest.mark.parametrize("facade_name", FACADES)
def test_facade_all_matches_its_exports(facade_name):
    facade = importlib.import_module(facade_name)

    assert facade.__all__ == sorted(facade._EXPORTS)
    assert dir(facade) == facade.__all__


@pytest.mark.parametrize("facade_name", FACADES)
def test_unknown_attribute_raises_attribute_error(facade_name):
    facade = importlib.import_module(facade_name)

    with pytest.raises(AttributeError, match="no attribute"):
        facade.DefinitelyNotExported


@pytest.mark.parametrize("facade_name", FACADES)
def test_resolution_is_cached_in_module_globals(facade_name):
    facade = importlib.import_module(facade_name)
    name = facade.__all__[0]

    first = getattr(facade, name)
    assert name in vars(facade)
    assert getattr(facade, name) is first


def test_cqrs_facade_exposes_only_the_canonical_names():
    """
    S4: un solo nombre por concepto en la superficie que enseña la documentación. Los
    alias `I*` siguen existiendo en su módulo, pero no aquí.
    """
    import hexcore.cqrs as cqrs

    assert "AbstractCommandBus" in cqrs.__all__
    for legacy in ("ICommandBus", "IQueryBus", "IEventBus", "ISerializer", "IMiddleware"):
        assert legacy not in cqrs.__all__, f"{legacy} no debería estar en la fachada"

    # Pero el alias sigue importable por su ruta de siempre.
    from hexcore.domain.cqrs.buses import ICommandBus

    assert ICommandBus is cqrs.AbstractCommandBus


def test_cqrs_facade_covers_the_smart_routing_workflow():
    """El caso de uso que motivó la fachada: montar CQRS con un solo import."""
    import hexcore.cqrs as cqrs

    registry = cqrs.HandlerRegistry()
    bus = cqrs.InMemoryCommandBus(
        registry=registry,
        enqueuer=None,
        serializer=cqrs.PydanticSerializer(),
    )

    assert bus is not None
    assert callable(cqrs.background_command)
    assert cqrs.CQRSConsumer is not None


@pytest.mark.parametrize(
    ("facade_name", "hidden"),
    [
        ("hexcore.cqrs", "fastapi"),
        ("hexcore.cqrs", "sqlalchemy"),
        ("hexcore.cqrs", "redis"),
        ("hexcore.cqrs", "procrastinate"),
        ("hexcore.sql", "fastapi"),
        ("hexcore.sql", "sqlalchemy"),
        ("hexcore.fastapi", "sqlalchemy"),
        ("hexcore.fastapi", "redis"),
    ],
)
def test_importing_a_facade_does_not_require_optional_dependencies(facade_name, hidden):
    """
    S5: nada de I/O ni imports pesados en import time. La resolución perezosa es lo que
    permite que `import hexcore.cqrs` funcione sin el extra `[sql]`.
    """
    code = f"""
import sys

class Blocker:
    @classmethod
    def find_spec(cls, fullname, path, target=None):
        if fullname.split(".")[0] == "{hidden}":
            raise ImportError("bloqueado: " + fullname)
        return None

sys.meta_path.insert(0, Blocker)

import {facade_name}
assert {facade_name}.__all__
print("ok")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_hexcore_fastapi_does_not_shadow_the_real_fastapi():
    """
    El módulo se llama `hexcore.fastapi`; con imports absolutos (el default en Python 3)
    los módulos internos siguen viendo el paquete `fastapi` real. Vale comprobarlo.
    """
    pytest.importorskip("fastapi")

    import fastapi

    import hexcore.fastapi as hx

    assert fastapi.__name__ == "fastapi"
    assert hx.__name__ == "hexcore.fastapi"
    assert hx.create_app.__module__ == "hexcore.infrastructure.api.app"


def test_facades_do_not_execute_io_on_import():
    """
    Importar las tres fachadas no debe crear engines, conexiones ni leer configuración.
    """
    code = """
import hexcore.cqrs, hexcore.sql, hexcore.fastapi
from hexcore.infrastructure.repositories.orms.sqlalchemy import session
assert session._engine is None, "importar las fachadas creó un engine"
from hexcore.config import LazyConfig
assert LazyConfig._imported_config is None, "importar las fachadas resolvió la config"
print("ok")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
