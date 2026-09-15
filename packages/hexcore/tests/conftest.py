import os

from hexcore.config import LazyConfig
from hexcore.infrastructure.api.eventsourcing import reset_event_store
from hexcore.infrastructure.repositories.utils import clear_discovery_cache


_CONFIG_ENV_KEYS = ("HEXCORE_CONFIG_MODULE", "HEXCORE_CONFIG_MODULES")


def pytest_runtest_setup(item) -> None:
    del item
    clear_discovery_cache()
    LazyConfig.clear_cache()
    # Sin esto, el contenedor que configuro un test lo ve el siguiente: el almacen en
    # memoria llegaria con los eventos de otro caso, y el orden de ejecucion pasaria a
    # importar. Es el mismo defecto que tenia ServerConfig.event_bus con su instancia
    # compartida a nivel de modulo.
    reset_event_store()


def pytest_runtest_teardown(item, nextitem) -> None:
    del item, nextitem
    clear_discovery_cache()
    LazyConfig.clear_cache()
    reset_event_store()
    for key in _CONFIG_ENV_KEYS:
        os.environ.pop(key, None)
