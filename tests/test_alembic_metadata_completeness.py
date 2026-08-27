"""
Prerequisito: `Base.metadata` tiene que estar completo antes de una migración.

El bug: `alembic revision --autogenerate` compara la base contra `Base.metadata` y emite
`op.drop_table` para toda tabla que exista en la base y falte en el metadata. Dos agujeros
lo hacían pasar:

1. `import_all_models` usaba `pkgutil.iter_modules`, que **no** recorre subpaquetes: un
   modelo en ``models/billing/invoice.py`` nunca se importaba.
2. Nada importaba los modelos que declara **HexCore**. `CronSeedStep(create_tables=True)`
   crea `hexcore_cron_jobs` en la base, `CronJobModel` no se importa en ningún momento del
   `env.py` generado, y la migración siguiente lo dropea.

El segundo es el grave y **ya afecta a este framework hoy**. Con el módulo de identidad
serían `darwin_user`, `darwin_session`, `darwin_account` y `darwin_verification`: todo el
almacén de credenciales, borrado por una migración de rutina.
"""
from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("sqlalchemy")

from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import (  # noqa: E402
    ensure_framework_models_loaded,
    import_all_models,
)


# ── 1. El recorrido es recursivo ──────────────────────────────────────────────
@pytest.fixture
def paquete_anidado(tmp_path, monkeypatch):
    """
    Un paquete `modelos_falsos/` con un submódulo anidado dos niveles.

        modelos_falsos/__init__.py
        modelos_falsos/plano.py
        modelos_falsos/billing/__init__.py
        modelos_falsos/billing/invoice.py      <- el que `iter_modules` nunca veía
    """
    raiz = tmp_path / "modelos_falsos"
    (raiz / "billing").mkdir(parents=True)
    (raiz / "__init__.py").write_text("", encoding="utf-8")
    (raiz / "plano.py").write_text("CARGADO = True\n", encoding="utf-8")
    (raiz / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (raiz / "billing" / "invoice.py").write_text("CARGADO = True\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(tmp_path))
    for nombre in [m for m in sys.modules if m.startswith("modelos_falsos")]:
        del sys.modules[nombre]

    import modelos_falsos

    yield modelos_falsos

    for nombre in [m for m in sys.modules if m.startswith("modelos_falsos")]:
        del sys.modules[nombre]


def test_import_all_models_recorre_subpaquetes(paquete_anidado: types.ModuleType):
    """La regresión: con `iter_modules`, `billing.invoice` quedaba sin importar."""
    importados = import_all_models(paquete_anidado)

    assert "modelos_falsos.plano" in importados
    assert "modelos_falsos.billing.invoice" in importados, (
        "un modelo en un subpaquete no se importó: su tabla quedaría afuera de "
        "Base.metadata y --autogenerate le emitiría un DROP TABLE"
    )
    assert "modelos_falsos.billing.invoice" in sys.modules


def test_import_all_models_no_se_traga_los_errores(tmp_path, monkeypatch):
    """
    Un módulo que no importa tiene que explotar, no saltarse.

    Tragarse el error significa una tabla ausente del metadata y un DROP TABLE en la
    migración. Enterarse acá es incómodo; enterarse después es pérdida de datos.
    """
    raiz = tmp_path / "modelos_rotos"
    raiz.mkdir()
    (raiz / "__init__.py").write_text("", encoding="utf-8")
    (raiz / "roto.py").write_text("import modulo_que_no_existe\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(tmp_path))
    import modelos_rotos

    with pytest.raises(ImportError):
        import_all_models(modelos_rotos)


# ── 2. Los modelos del framework entran al metadata ───────────────────────────
def test_las_tablas_del_framework_entran_al_metadata():
    """
    El escenario que hoy borra `hexcore_cron_jobs`.

    Se afirma sobre `Base.metadata` porque es literalmente lo que Alembic compara: si la
    tabla no está ahí, `--autogenerate` la dropea.
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

    ensure_framework_models_loaded()

    assert "hexcore_cron_jobs" in Base.metadata.tables, (
        "hexcore_cron_jobs no está en Base.metadata: `alembic revision --autogenerate` "
        "va a emitir op.drop_table sobre la tabla de cron jobs"
    )


def test_ensure_framework_models_loaded_es_idempotente():
    primera = ensure_framework_models_loaded()
    segunda = ensure_framework_models_loaded()

    assert primera == segunda


def test_ensure_framework_models_loaded_no_explota_sin_el_extra(monkeypatch):
    """
    Sin `[sql]` no hay `Base`, así que no hay metadata que poblar y no es un error.

    Se simula ocultando el módulo: la función tiene que devolver una lista vacía, no
    propagar el ImportError.
    """
    monkeypatch.setitem(
        sys.modules, "hexcore.infrastructure.cqrs.cron_sql", None
    )
    # `sys.modules[x] = None` hace que el import lance ImportError, que es exactamente lo
    # que pasa cuando el extra no está instalado.
    assert ensure_framework_models_loaded() == []


# ── 3. El env.py generado usa las dos ─────────────────────────────────────────
def test_el_env_py_generado_carga_las_dos_familias_de_modelos():
    """
    El fix no sirve si el `env.py` que genera `hexcore init` no lo llama.

    Se inspecciona el fuente de `_setup_alembic` en vez de correr `alembic init`, que
    necesitaría alembic instalado y escribiría en disco.
    """
    import inspect

    from hexcore.infrastructure import cli

    fuente = inspect.getsource(cli._setup_alembic)

    assert "ensure_framework_models_loaded()" in fuente, (
        "el env.py generado no carga los modelos del framework: las tablas de HexCore "
        "quedarían afuera de Base.metadata"
    )
    assert "import_all_models(models)" in fuente
    # El orden importa poco funcionalmente, pero el del framework va primero para que un
    # fallo al importar los del consumidor no deje el metadata a medias.
    assert fuente.index("ensure_framework_models_loaded()") < fuente.index(
        "import_all_models(models)"
    )


def test_la_fachada_sql_expone_las_dos():
    """Van por `hexcore.sql`: el env.py generado no debería importar rutas internas."""
    import hexcore.sql as sql

    assert callable(sql.import_all_models)
    assert callable(sql.ensure_framework_models_loaded)
    assert "import_all_models" in sql.__all__
    assert "ensure_framework_models_loaded" in sql.__all__
