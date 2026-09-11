"""
hexcore.application.eventsourcing - lo que orquesta los puertos del event store.

El repositorio event-sourced, el proyector y el relay. Todo aca depende solo de los puertos
de `hexcore.domain.eventsourcing`, nunca de un adaptador concreto, asi que este paquete
importa sin ningun extra instalado.
"""
from __future__ import annotations

from .projector import Projector
from .relay import EventStoreRelay
from .repository import EventSourcedRepository

__all__ = ["EventSourcedRepository", "Projector", "EventStoreRelay"]
