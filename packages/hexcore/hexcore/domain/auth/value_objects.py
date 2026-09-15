import typing as t

from uuid import uuid4
from pydantic import Field, BaseModel
from enum import Enum


class TokenClaims(BaseModel):
    """Detalles sobre los claims de un token."""

    iss: str  # Identificador del token de acceso
    sub: str  # ID del usuario
    exp: int  # Tiempo de expiración
    iat: int  # Tiempo de emisión
    jti: str = Field(default_factory=lambda: str(uuid4()))  # ID del token
    client_id: str  # ID del cliente OAuth
    scopes: t.List[Enum] = []  # Permisos del Token
