from abc import ABC
from pydantic import BaseModel

class DTO(BaseModel, ABC): ...
