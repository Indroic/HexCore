import typing as t
import abc


class ICache(abc.ABC):
    @abc.abstractmethod
    async def get(self, key: str) -> t.Optional[t.Any]:
        pass

    @abc.abstractmethod
    def set(self, key: str, value: t.Any, expire: int = 3600) -> t.Any:
        pass

    @abc.abstractmethod
    def delete(self, key: str) -> t.Union[t.Any, None]:
        pass
