from abc import ABC
from pydantic import BaseModel


class DTO(BaseModel, ABC):
    """
    Clase base para todos los DTOs de la capa de aplicación.

    Estos representan los datos que se desea exponer o recibir a través de la API,
    evitando la exposición de detalles internos del dominio.
    """

    pass
