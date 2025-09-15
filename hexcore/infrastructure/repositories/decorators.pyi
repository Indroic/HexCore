from typing import Any, Awaitable, Callable, TypeVar

A = TypeVar('A')

def cycle_protection_resolver(func: Callable[[A], Awaitable[Any]]) -> Callable[[A], Awaitable[Any]]: ...
