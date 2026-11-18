"""
La versión de UUID de los ids que genera el framework: global y por clase.

Lo que se fija:

1. El default sigue siendo v4 — cambiarlo sería un cambio de comportamiento silencioso.
2. El global se resuelve **en cada llamada** (clases ya definidas lo ven) y el por-clase gana y
   se hereda.
3. Llega a todas las superficies: entidad, agregado, evento, comando y modelo SQL.
4. Los generadores propios (los de Python < 3.14) producen v6/v7 válidos y v7 ordenado.
5. Las versiones sin sentido sin datos extra (3, 5) y las inválidas fallan con un mensaje claro.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from hexcore.domain import identifiers
from hexcore.domain.base import BaseEntity
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing.aggregate import AggregateRoot
from hexcore.domain.identifiers import (
    UnsupportedUUIDVersionError,
    get_default_uuid_version,
    new_uuid,
    set_default_uuid_version,
    using_uuid_version,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base, BaseModel


@pytest.fixture(autouse=True)
def _sin_global(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(identifiers.ENV_UUID_VERSION, raising=False)
    set_default_uuid_version(None)
    yield
    set_default_uuid_version(None)


class Pedido(BaseEntity):
    pass


class PedidoV7(BaseEntity, uuid_version=7):
    pass


class PedidoV7Hijo(PedidoV7):
    pass


class PedidoV6Hijo(PedidoV7, uuid_version=6):
    pass


class Carrito(AggregateRoot, uuid_version=7):
    pass


class Evento(DomainEvent):
    pass


class Comando(Command):
    pass


# ── Default y global ──────────────────────────────────────────────────────────


def test_el_default_sigue_siendo_v4() -> None:
    assert get_default_uuid_version() == 4
    assert Pedido().id.version == 4
    assert Evento().event_id.version == 4
    assert Comando().command_id.version == 4


def test_el_global_se_resuelve_en_cada_llamada_incluso_en_clases_ya_definidas() -> None:
    assert Pedido().id.version == 4
    set_default_uuid_version(7)
    assert Pedido().id.version == 7
    assert Evento().event_id.version == 7
    assert Comando().command_id.version == 7
    set_default_uuid_version(None)
    assert Pedido().id.version == 4


def test_el_entorno_fija_el_global_y_el_codigo_gana(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(identifiers.ENV_UUID_VERSION, "7")
    assert Pedido().id.version == 7
    set_default_uuid_version(6)
    assert Pedido().id.version == 6


def test_un_entorno_invalido_falla_nombrando_la_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(identifiers.ENV_UUID_VERSION, "5")
    with pytest.raises(UnsupportedUUIDVersionError, match=identifiers.ENV_UUID_VERSION):
        Pedido()


def test_using_uuid_version_restaura_al_salir_incluso_con_error() -> None:
    set_default_uuid_version(6)
    with pytest.raises(RuntimeError):
        with using_uuid_version(7):
            assert Pedido().id.version == 7
            raise RuntimeError
    assert get_default_uuid_version() == 6


# ── Por clase ─────────────────────────────────────────────────────────────────


def test_la_version_de_clase_gana_sobre_el_global_y_se_hereda() -> None:
    set_default_uuid_version(4)
    assert PedidoV7().id.version == 7
    assert PedidoV7Hijo().id.version == 7
    assert PedidoV6Hijo().id.version == 6
    # El resto de la jerarquía no se contamina.
    assert Pedido().id.version == 4
    assert BaseEntity().id.version == 4


def test_un_id_explicito_no_se_toca() -> None:
    fijo = uuid.uuid4()
    assert PedidoV7(id=fijo).id == fijo


def test_el_agregado_acepta_la_version_de_clase() -> None:
    assert Carrito().id.version == 7
    assert AggregateRoot().id.version == 4


def test_una_version_de_clase_invalida_falla_al_definirla() -> None:
    with pytest.raises(UnsupportedUUIDVersionError):

        class _Mala(BaseEntity, uuid_version=5):  # pyright: ignore[reportCallIssue]
            pass


# ── SQL ───────────────────────────────────────────────────────────────────────


class _Fila(BaseModel[PedidoV7]):
    __tablename__ = "uuidv_fila"
    nombre: Mapped[str] = mapped_column(default="")


class _FilaV7(BaseModel[PedidoV7], uuid_version=7):
    __tablename__ = "uuidv_fila_v7"


class _BaseAbstractaV6(BaseModel[PedidoV7], uuid_version=6):
    __abstract__ = True


class _FilaV6Heredada(_BaseAbstractaV6):
    __tablename__ = "uuidv_fila_v6"


def test_modelo_sql_global_por_clase_y_heredado() -> None:
    async def _correr() -> dict[str, int]:
        motor = create_async_engine("sqlite+aiosqlite://")
        async with motor.begin() as conexion:
            await conexion.run_sync(Base.metadata.create_all)
        sesiones = async_sessionmaker(motor, expire_on_commit=False)
        versiones: dict[str, int] = {}
        async with sesiones() as sesion:
            normal, v7, v6 = _Fila(), _FilaV7(), _FilaV6Heredada()
            sesion.add_all([normal, v7, v6])
            await sesion.flush()
            versiones["normal"] = normal.id.version or 0
            versiones["v7"] = v7.id.version or 0
            versiones["v6"] = v6.id.version or 0

            with using_uuid_version(7):
                otra = _Fila()
                sesion.add(otra)
                await sesion.flush()
                versiones["normal_con_global_7"] = otra.id.version or 0

            # Round trip: el id v7 se persiste y se lee igual.
            leido = (await sesion.execute(select(_FilaV7.id))).scalar_one()
            assert leido == v7.id
        await motor.dispose()
        return versiones

    assert asyncio.run(_correr()) == {
        "normal": 4,
        "v7": 7,
        "v6": 6,
        "normal_con_global_7": 7,
    }


def test_una_version_de_modelo_invalida_falla_al_definirlo() -> None:
    with pytest.raises(UnsupportedUUIDVersionError):

        class _Mala(BaseModel[PedidoV7], uuid_version=3):  # pyright: ignore[reportCallIssue]
            __tablename__ = "uuidv_mala"


# ── Generadores ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("version", [1, 4, 6, 7])
def test_new_uuid_genera_la_version_pedida(version: int) -> None:
    generado = new_uuid(version)
    assert generado.version == version
    assert generado.variant == uuid.RFC_4122


@pytest.mark.parametrize("version", [3, 5, 8, 0, 99, "x", True])
def test_versiones_no_generables_se_rechazan(version: object) -> None:
    with pytest.raises(UnsupportedUUIDVersionError):
        new_uuid(version)  # pyright: ignore[reportArgumentType]


def test_v3_y_v5_explican_por_que() -> None:
    with pytest.raises(UnsupportedUUIDVersionError, match="espacio de nombres"):
        new_uuid(5)


def test_fallback_de_v7_para_python_sin_uuid7(monkeypatch: pytest.MonkeyPatch) -> None:
    """Python < 3.14 no trae `uuid.uuid7`: el generador propio tiene que bastar."""
    monkeypatch.delattr(uuid, "uuid7", raising=False)
    ids = [new_uuid(7) for _ in range(5000)]
    assert all(i.version == 7 and i.variant == uuid.RFC_4122 for i in ids)
    assert len(set(ids)) == len(ids)
    # Creciente aun dentro del mismo milisegundo: es lo que justifica usar v7 de PK.
    assert ids == sorted(ids)


def test_fallback_de_v6_para_python_sin_uuid6(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(uuid, "uuid6", raising=False)
    ids = [new_uuid(6) for _ in range(500)]
    assert all(i.version == 6 and i.variant == uuid.RFC_4122 for i in ids)
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


def test_el_v7_propio_lleva_el_timestamp_en_los_bits_altos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(uuid, "uuid7", raising=False)
    monkeypatch.setattr(identifiers, "_ultimo_ms_v7", 0)
    monkeypatch.setattr(identifiers.time, "time_ns", lambda: 1_700_000_000_123 * 1_000_000)
    generado = new_uuid(7)
    assert generado.int >> 80 == 1_700_000_000_123
