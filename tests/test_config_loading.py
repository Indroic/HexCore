import os
import typing as t
from types import SimpleNamespace
from unittest.mock import patch

from hexcore.config import LazyConfig, ServerConfig


def _reset_lazy_config_state() -> None:
    LazyConfig.clear_cache()
    LazyConfig.set_config_modules(("config",))


def _collect_attempted_modules() -> tuple[list[str], t.Callable[[str], object]]:
    attempted: list[str] = []

    def _side_effect(module_name: str) -> object:
        attempted.append(module_name)
        raise ModuleNotFoundError(module_name)

    return attempted, _side_effect


def test_get_config_prioritizes_env_single_module() -> None:
    _reset_lazy_config_state()
    LazyConfig.set_config_modules(("my.project.config",))
    attempted, side_effect = _collect_attempted_modules()

    with (
        patch.dict(
            os.environ,
            {
                "HEXCORE_CONFIG_MODULE": "custom.root_config",
                "HEXCORE_CONFIG_MODULES": "a.config,b.config",
            },
            clear=False,
        ),
        patch("hexcore.config.importlib.import_module", side_effect=side_effect),
    ):
        LazyConfig.get_config()

    assert attempted == ["custom.root_config"]


def test_get_config_prioritizes_env_modules_list() -> None:
    _reset_lazy_config_state()
    LazyConfig.set_config_modules(("my.project.config",))
    attempted, side_effect = _collect_attempted_modules()

    with (
        patch.dict(
            os.environ,
            {
                "HEXCORE_CONFIG_MODULE": "",
                "HEXCORE_CONFIG_MODULES": "a.config, b.config , ,c.config",
            },
            clear=False,
        ),
        patch("hexcore.config.importlib.import_module", side_effect=side_effect),
    ):
        LazyConfig.get_config()

    assert attempted == ["a.config", "b.config", "c.config"]


def test_get_config_uses_set_modules_when_env_is_empty() -> None:
    _reset_lazy_config_state()
    LazyConfig.set_config_modules(("project.settings", "project.extra"))
    attempted, side_effect = _collect_attempted_modules()

    with (
        patch.dict(
            os.environ,
            {"HEXCORE_CONFIG_MODULE": "", "HEXCORE_CONFIG_MODULES": ""},
            clear=False,
        ),
        patch("hexcore.config.importlib.import_module", side_effect=side_effect),
    ):
        LazyConfig.get_config()

    assert attempted == ["project.settings", "project.extra"]


def test_clear_cache_invalidates_cached_instance() -> None:
    _reset_lazy_config_state()
    first = ServerConfig(host="cached-host")
    second = ServerConfig(host="second-host")
    modules = [SimpleNamespace(config=first), SimpleNamespace(config=second)]

    with (
        patch.dict(
            os.environ,
            {"HEXCORE_CONFIG_MODULE": "project.config", "HEXCORE_CONFIG_MODULES": ""},
            clear=False,
        ),
        patch("hexcore.config.importlib.import_module", side_effect=modules),
    ):
        loaded_1 = LazyConfig.get_config()
        LazyConfig.clear_cache()
        loaded_2 = LazyConfig.get_config()

    assert loaded_1 is first
    assert loaded_2 is second


def test_get_config_uses_config_instance_from_module() -> None:
    _reset_lazy_config_state()

    custom = ServerConfig(host="custom-instance")
    module = SimpleNamespace(config=custom)

    with (
        patch.dict(
            os.environ,
            {"HEXCORE_CONFIG_MODULE": "myapp.config", "HEXCORE_CONFIG_MODULES": ""},
            clear=False,
        ),
        patch("hexcore.config.importlib.import_module", return_value=module),
    ):
        loaded = LazyConfig.get_config()

    assert loaded is custom


def test_get_config_uses_server_config_class_from_module() -> None:
    _reset_lazy_config_state()

    class ProjectServerConfig(ServerConfig):
        pass

    module = SimpleNamespace(ServerConfig=ProjectServerConfig)

    with (
        patch.dict(
            os.environ,
            {
                "HEXCORE_CONFIG_MODULE": "myapp.config_class",
                "HEXCORE_CONFIG_MODULES": "",
            },
            clear=False,
        ),
        patch("hexcore.config.importlib.import_module", return_value=module),
    ):
        loaded = LazyConfig.get_config()

    assert isinstance(loaded, ProjectServerConfig)


def test_get_config_falls_back_to_default_when_module_not_found() -> None:
    _reset_lazy_config_state()

    with (
        patch.dict(
            os.environ,
            {"HEXCORE_CONFIG_MODULE": "missing.module", "HEXCORE_CONFIG_MODULES": ""},
            clear=False,
        ),
        patch(
            "hexcore.config.importlib.import_module",
            side_effect=ModuleNotFoundError("missing.module"),
        ),
    ):
        loaded = LazyConfig.get_config()

    assert isinstance(loaded, ServerConfig)


def test_get_config_uses_cached_value_after_first_resolution() -> None:
    _reset_lazy_config_state()

    first = ServerConfig(host="first")
    second = ServerConfig(host="second")
    modules = [SimpleNamespace(config=first), SimpleNamespace(config=second)]

    with (
        patch.dict(
            os.environ,
            {"HEXCORE_CONFIG_MODULE": "project.config", "HEXCORE_CONFIG_MODULES": ""},
            clear=False,
        ),
        patch("hexcore.config.importlib.import_module", side_effect=modules),
    ):
        loaded_1 = LazyConfig.get_config()
        loaded_2 = LazyConfig.get_config()

    assert loaded_1 is first
    assert loaded_2 is first
