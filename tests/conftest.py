import os

from hexcore.config import LazyConfig
from hexcore.infrastructure.repositories.utils import clear_discovery_cache


_CONFIG_ENV_KEYS = ("HEXCORE_CONFIG_MODULE", "HEXCORE_CONFIG_MODULES")


def pytest_runtest_setup(item) -> None:
    del item
    clear_discovery_cache()
    LazyConfig.clear_cache()


def pytest_runtest_teardown(item, nextitem) -> None:
    del item, nextitem
    clear_discovery_cache()
    LazyConfig.clear_cache()
    for key in _CONFIG_ENV_KEYS:
        os.environ.pop(key, None)
