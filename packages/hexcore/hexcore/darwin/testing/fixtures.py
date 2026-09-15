"""
Fixtures de pytest para Darwin.

Para usarlas, agregá en tu `conftest.py`::

    pytest_plugins = ["hexcore.testing.fixtures", "hexcore.darwin.testing.fixtures"]

⚠️ **`identity_container` resetea el contenedor al terminar, y eso no es opcional.** El contenedor
es global del proceso: un test que lo cablea y no limpia le deja su cableado al siguiente, y el
síntoma es el peor de todos — cada test pasa aislado y la suite falla, con el error apuntando a un
archivo que no tiene nada que ver.

Y `identity_container` **también resetea el `cache_backend`**, por el mismo motivo con otra causa:
`rate_limit` usa `config.cache_backend`, que es un `MemoryCache` global. Sin resetearlo, el contador
de intentos de login se acumula entre tests y del sexto en adelante todo da 429.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime

import pytest

__all__ = [
    "AHORA_DE_TEST",
    "identity_clock",
    "identity_container",
    "identity_audit",
    "identity_users",
]

#: El instante de los tests. Fijo y reproducible: un reloj que arranca en `now()` hace que un test
#: de vencimiento falle una vez al año, cuando el cambio de horario mueve el offset.
AHORA_DE_TEST = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def identity_clock() -> t.Any:
    """
    Un `FixedClock` en `AHORA_DE_TEST`.

    Se expone aparte para que el test pueda adelantarlo: `identity_clock.advance(minutes=5)`.
    """
    from hexcore.darwin.infrastructure.clock import FixedClock

    return FixedClock(AHORA_DE_TEST)


@pytest.fixture
def identity_audit() -> t.Any:
    """
    Un `RecordingAuditSink`.

    Cableado siempre, incluso en los tests que no lo miran: es barato, y tenerlo puesto evita que
    un test que *sí* debería aseverar sobre la auditoría no pueda porque el contenedor se cableó
    sin sink.
    """
    from hexcore.darwin.testing.fakes import RecordingAuditSink

    return RecordingAuditSink()


@pytest.fixture
def identity_users() -> list[t.Any]:
    """
    Los usuarios a sembrar. Vacío por default; sobreescribila en tu módulo para sembrar.

    Uso::

        @pytest.fixture
        def identity_users():
            return [make_user("ana@ejemplo.com"), make_user("beto@ejemplo.com")]
    """
    return []


@pytest.fixture
def identity_container(
    identity_clock: t.Any, identity_audit: t.Any, identity_users: list[t.Any]
) -> t.Iterator[t.Any]:
    """
    Darwin cableado con los fakes, y limpiado al terminar.

    Sin base, sin tablas y sin Argon2. Ver `configure_test_identity` para qué inyecta y por qué.

    Uso::

        async def test_mi_caso(identity_container):
            servicio = identity_container.identity_service()
            usuario, _ = await servicio.sign_up(email="ana@x.com", password="una frase")
    """
    from hexcore.config import LazyConfig
    from hexcore.darwin.application.container import reset_identity
    from hexcore.darwin.testing.helpers import configure_test_identity
    from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

    # Ver la advertencia del docstring del módulo: `rate_limit` usa este backend global.
    LazyConfig.get_config().cache_backend = MemoryCache()

    reset_identity()
    contenedor = configure_test_identity(
        seed_users=identity_users, clock=identity_clock, audit=identity_audit
    )
    try:
        yield contenedor
    finally:
        reset_identity()
