import contextvars
from typing import Any, Callable, Awaitable, TypeVar

A = TypeVar("A")
# ContextVars para stack de ids y cache de resultados
_visited_ctx: contextvars.ContextVar[set[int]] = contextvars.ContextVar(
    "visited", default=set()
)
_results_ctx: contextvars.ContextVar[dict[int, Any]] = contextvars.ContextVar(
    "results", default={}
)


def cycle_protection_resolver(
    func: Callable[[A], Awaitable[Any]],
) -> Callable[[A], Awaitable[Any]]:
    """
    Decorador para resolvedores asíncronos que protege contra recursividad infinita en relaciones cíclicas.

    Mecanismo:
        1. Cada vez que se llama al resolvedor decorado, se crean dos estructuras internas:
            - visited: un set que almacena los ids de las entidades ya visitadas en la cadena de resolución actual.
            - visited_results: un diccionario que almacena el resultado ya calculado para cada id de entidad.
        2. Cuando el resolvedor es llamado con una entidad:
            - Si el id de la entidad ya está en visited, significa que se está entrando en un ciclo. En ese caso, el decorador retorna el resultado previamente calculado para ese id (o None si no existe), evitando así la recursión infinita.
            - Si no está en visited, agrega el id y ejecuta el resolvedor normalmente.
            - Al terminar, almacena el resultado en visited_results para ese id.
        3. Así, aunque los resolvedores se llamen recursivamente (por ejemplo, en relaciones A → B → A), nunca se entra en un bucle infinito porque el decorador corta la recursión y retorna el valor ya calculado.
    """

    async def wrapper(model: A) -> Any:
        entity_id = getattr(model, "id", None)
        visited = _visited_ctx.get().copy()
        results = _results_ctx.get().copy()
        token_visited = _visited_ctx.set(visited)
        token_results = _results_ctx.set(results)
        try:
            if entity_id is not None:
                if entity_id in visited:
                    return results.get(entity_id)
                visited.add(entity_id)
            result = await func(model)
            if entity_id is not None:
                results[entity_id] = result
            return result
        finally:
            _visited_ctx.reset(token_visited)
            _results_ctx.reset(token_results)

    return wrapper
