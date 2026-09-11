"""
Fija el contrato observable de `BaseEntity` antes de moverle la base.

`BaseEntity` es la clase mas usada del framework: la heredan todas las entidades de dominio
del consumidor, la mapean los repositorios, y `SqlAlchemyUnitOfWork.collect_domain_entities()`
la reconoce con un `isinstance`. Extraer su capacidad de acumular eventos a una base propia
(`EventRecorder`, para compartirla con `AggregateRoot`) es un refactor seguro **solo si**
nada de eso cambia, y "nada de eso cambia" hay que poder demostrarlo.

Este archivo se escribio **antes** del refactor y paso contra el codigo anterior. Lo que fija:

1. Los campos declarados y su orden. Un campo perdido en el camino es un error de datos
   silencioso; un campo *nuevo* heredado de la base tambien, porque `model_dump()` lo
   arrastra a cada serializacion y a cada fila del ORM.
2. La configuracion de pydantic. `from_attributes` es lo que permite construir entidades
   desde modelos de SQLAlchemy y `validate_assignment` es lo que revalida al mutar: los dos
   son comportamiento, no estilo.
3. Que `BaseEntity` sigue siendo `BaseModel` y sigue siendo compatible con `abc.ABC` via
   `AbstractModelMeta` \u2014 la combinacion de metaclases que ese modulo existe para resolver.
4. **El aislamiento de `_domain_events` entre instancias.** El default era una lista mutable
   en el cuerpo de la clase. Hoy no es un defecto vivo porque pydantic hace `smart_deepcopy`
   del default de un `ModelPrivateAttr`, pero es una trampa que depende de un detalle interno
   de pydantic: si esa optimizacion cambiara, dos entidades compartirian la lista y los
   eventos de una se despacharian con los de la otra. El test vale contra las dos formas.
"""
from __future__ import annotations

import abc
import typing as t
from datetime import datetime
from uuid import UUID

import pytest
from pydantic import BaseModel

from hexcore.domain.base import AbstractModelMeta, BaseEntity
from hexcore.domain.events import DomainEvent


class _EventoDePrueba(DomainEvent):
    dato: str = "x"


class TestLaSuperficieDeBaseEntity:
    def test_los_campos_declarados_y_su_orden_no_cambian(self):
        assert list(BaseEntity.model_fields) == [
            "id",
            "created_at",
            "updated_at",
            "is_active",
        ], (
            "cambio la superficie de campos de BaseEntity: todo consumidor la hereda, "
            "asi que un campo de mas o de menos altera cada model_dump() y cada fila del ORM."
        )

    def test_los_tipos_de_los_campos_no_cambian(self):
        anotaciones = {
            nombre: campo.annotation for nombre, campo in BaseEntity.model_fields.items()
        }
        assert anotaciones["id"] is UUID
        assert anotaciones["created_at"] is datetime
        assert anotaciones["updated_at"] is datetime
        assert anotaciones["is_active"] == t.Optional[bool]

    def test_la_configuracion_de_pydantic_no_cambia(self):
        config = BaseEntity.model_config
        assert config.get("from_attributes") is True, (
            "sin from_attributes no se pueden construir entidades desde modelos del ORM"
        )
        assert config.get("validate_assignment") is True, (
            "sin validate_assignment una asignacion invalida no se detecta hasta el guardado"
        )
        assert config.get("frozen") is False, (
            "BaseEntity es mutable a proposito: el ORM modifica y guarda el mismo objeto"
        )

    def test_sigue_siendo_un_modelo_de_pydantic(self):
        assert issubclass(BaseEntity, BaseModel)

    def test_sigue_combinandose_con_abc(self):
        """`AbstractModelMeta` existe para resolver el choque de metaclases pydantic/ABC."""
        assert issubclass(AbstractModelMeta, BaseEntity)
        assert issubclass(AbstractModelMeta, abc.ABC)

        class Concreta(AbstractModelMeta):
            async def hacer(self) -> str:
                return "hecho"

        assert Concreta().id is not None

    def test_los_defaults_siguen_generandose_por_instancia(self):
        una, otra = BaseEntity(), BaseEntity()
        assert una.id != otra.id, "el id tiene que salir de un default_factory, no ser fijo"
        assert una.is_active is True


class TestElAcumuladorDeEventos:
    def test_registrar_y_drenar(self):
        entidad = BaseEntity()
        evento = _EventoDePrueba()
        entidad.register_event(evento)

        drenados = entidad.pull_domain_events()

        assert drenados == [evento]
        assert entidad.pull_domain_events() == [], "pull tiene que dejar la lista vacia"

    def test_limpiar_sin_entregar(self):
        entidad = BaseEntity()
        entidad.register_event(_EventoDePrueba())
        entidad.clear_domain_events()
        assert entidad.pull_domain_events() == []

    def test_dos_entidades_no_comparten_la_lista_de_eventos(self):
        """
        El invariante que hace segura la extraccion de `EventRecorder`.

        Si la lista fuera compartida, `collect_domain_events()` del UoW le sacaria a una
        entidad los eventos que registro otra, y se despacharian eventos de agregados que
        la transaccion ni toco.
        """
        una, otra = BaseEntity(), BaseEntity()
        evento = _EventoDePrueba()

        una.register_event(evento)

        assert otra.pull_domain_events() == [], (
            "las dos entidades comparten la lista de eventos: un evento registrado en una "
            "aparece en la otra"
        )
        assert una.pull_domain_events() == [evento]

    def test_una_subclase_tampoco_comparte_la_lista(self):
        class Pedido(BaseEntity):
            total: int = 0

        primero, segundo = Pedido(), Pedido()
        primero.register_event(_EventoDePrueba())

        assert segundo.pull_domain_events() == []

    def test_los_eventos_no_son_un_campo_del_modelo(self):
        """
        Si `_domain_events` fuera un campo, `model_dump()` lo serializaria y el ORM
        intentaria persistirlo como una columna.
        """
        assert "_domain_events" not in BaseEntity.model_fields
        assert "_domain_events" not in BaseEntity().model_dump()

    @pytest.mark.anyio
    async def test_deactivate_hace_borrado_logico(self):
        entidad = BaseEntity()
        await entidad.deactivate()
        assert entidad.is_active is False


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
