import typing as t
from functools import wraps

__all__ = ["register_entity_on_uow"]

#: El decorador devuelve **el mismo tipo** que recibe.
#:
#: Antes la firma era `Callable[[SelfRepoT, EntityT], Awaitable[EntityT]]` de entrada y de
#: salida, y eso rompía dos cosas a la vez en el método decorado: los parámetros pasaban a ser
#: posicionales —un `Callable[[A, B], C]` no tiene nombres— y el retorno pasaba de corrutina a
#: `Awaitable`, que es más ancho. Con eso, `BeanieRepository.save` dejaba de ser un override
#: válido de `IBaseRepository.save` y hacía falta un `# type: ignore` para taparlo.
#:
#: Un `TypeVar` acotado a `Callable` conserva la firma entera —nombres, defaults, y que sea
#: `async`—, que es el idioma para un decorador que no cambia la interfaz.
F = t.TypeVar("F", bound=t.Callable[..., t.Any])


def register_entity_on_uow(method: F) -> F:
    """
    Registra la entidad en el UoW después de guardarla, si juntó eventos de dominio.

    El chequeo es `getattr(entity, "_domain_events", None)` y no un `isinstance`: lo que
    importa es que la entidad tenga eventos para publicar, no de qué clase sea.
    """

    @wraps(method)
    async def wrapper(self: t.Any, entity: t.Any) -> t.Any:
        result = await method(self, entity)
        if getattr(entity, "_domain_events", None):
            self.uow.collect_entity(entity)
        return result

    # El `cast` es la contracara de tipar la entrada con un `TypeVar`: el envoltorio es una
    # función concreta y sólo el llamador sabe qué firma tenía el método original.
    return t.cast(F, wrapper)
