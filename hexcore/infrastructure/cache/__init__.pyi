import abc
import typing as t

class ICache(abc.ABC, metaclass=abc.ABCMeta):
    @abc.abstractmethod
    async def get(self, key: str) -> t.Any | None: ...
    @abc.abstractmethod
    def set(self, key: str, value: t.Any, expire: int = ...) -> t.Any: ...
    @abc.abstractmethod
    def delete(self, key: str) -> t.Any | None: ...
