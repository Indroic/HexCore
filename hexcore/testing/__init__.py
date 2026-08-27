"""
Utilidades de test para aplicaciones HexCore.

Nada de esto existía y todo el mundo lo iba a improvisar. Se importa **sin dependencias
opcionales duras**: `import hexcore.testing` funciona sin FastAPI, sin SQLAlchemy y sin
broker; lo que necesita algo concreto lo importa de forma perezosa y lo dice si falta.

Para las fixtures de pytest, añadí en tu `conftest.py`::

    pytest_plugins = ["hexcore.testing.fixtures"]
"""
from __future__ import annotations

from .fakes import (
    FakeLockProvider,
    InMemoryTaskEnqueuer,
    RecordedEnqueue,
)
from .helpers import build_test_buses, override_cqrs
from .repositories import FakeRepository, FakeUnitOfWork

__all__ = [
    "InMemoryTaskEnqueuer",
    "RecordedEnqueue",
    "FakeLockProvider",
    "FakeRepository",
    "FakeUnitOfWork",
    "override_cqrs",
    "build_test_buses",
]
