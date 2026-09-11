"""
Resolución de handlers por jerarquía de eventos.

Los buses de HexCore despachaban por **clase exacta** (`self._handlers.get(type(event))`).
Suscribirse a una clase base no recibía nada, y no fallaba: el handler quedaba registrado y
en silencio nunca se invocaba. Eso tenía dos consecuencias que este módulo cierra.

La primera es que la limitación del transporte terminó dictando la forma del dominio.
`hexcore/darwin/domain/events.py` declara sus catorce eventos como hojas concretas **sin una
clase base pública**, y lo dice explícitamente: con despacho exacto, un `AuthEvent` base no
serviría de nada. Un módulo de dominio no debería tener que aplanar su jerarquía porque el
bus no sabe recorrer un MRO.

La segunda es que un catch-all era imposible. El relay del event store y el `Projector`
necesitan suscribirse a `DomainEvent` y recibirlo *todo*; con despacho exacto habría que
registrar cada tipo concreto a mano y volver a hacerlo cada vez que alguien agrega uno.

Vive en `domain/` y no en `application/` porque no depende de nada: es una función sobre un
mapa. La comparten los tres buses (in-memory, Redis, Postgres), `AggregateRoot.apply` para
elegir el mutador de un evento, y `AbstractProjection.handles` para filtrar.
"""
from __future__ import annotations

import typing as t

from hexcore.domain.events import DomainEvent

__all__ = ["handlers_for", "matching_types"]

H = t.TypeVar("H")


def matching_types(event_type: type) -> list[type]:
    """
    Los tipos a los que un evento de `event_type` puede estar suscrito, del más específico
    al más general.

    Se recorre el MRO y se corta **en `DomainEvent` inclusive**: `BaseModel` y `object` están
    en el MRO de todo evento, pero no son puntos de suscripción legítimos. Un handler
    registrado contra `BaseModel` recibiría cada evento del sistema por accidente, y quien lo
    escribió casi seguro quiso otra cosa.
    """
    tipos: list[type] = []
    for candidato in event_type.__mro__:
        if not issubclass(candidato, DomainEvent):
            continue
        tipos.append(candidato)
        if candidato is DomainEvent:
            break
    return tipos


def handlers_for(
    event: DomainEvent,
    handlers: t.Mapping[type, t.Sequence[H]],
) -> list[H]:
    """
    Los handlers que corresponden a `event`, recorriendo su jerarquía.

    Args:
        event: El evento a despachar. Se usa su tipo; no se lo toca.
        handlers: El registro ``{tipo_de_evento: [handlers]}`` del bus.

    Returns:
        Los handlers, ordenados **de lo más específico a lo más general** y preservando el
        orden de suscripción dentro de cada tipo. Un handler suscrito a la clase exacta corre
        antes que uno suscrito a una base, que es el orden que espera quien escribe un
        catch-all de logging o de auditoría: lo específico primero, lo transversal después.

    **Deduplica por identidad**: un handler registrado contra la clase exacta *y* contra una
    base aparece una sola vez. Sin eso, un catch-all que además se suscribe a un tipo puntual
    correría dos veces por ese tipo, y un handler no idempotente haría daño real. Se
    deduplica por `id()` y no por igualdad porque un handler puede ser un objeto que define
    `__eq__` (un funcional parcial, un método ligado, un doble de test), y ahí la igualdad
    diría que dos suscripciones distintas son la misma.
    """
    if not handlers:
        return []

    resultado: list[H] = []
    vistos: set[int] = set()
    for tipo in matching_types(type(event)):
        for handler in handlers.get(tipo, ()):
            marca = id(handler)
            if marca in vistos:
                continue
            vistos.add(marca)
            resultado.append(handler)
    return resultado
