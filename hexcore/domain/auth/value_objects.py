import typing as t

from uuid import uuid4
from pydantic import Field, BaseModel
from .permissions import PermissionsEnum


class TokenClaims(BaseModel):
    """Detalles sobre los claims de un token."""

    iss: str  # Identificador del token de acceso
    sub: str  # ID del usuario
    exp: int  # Tiempo de expiración
    iat: int  # Tiempo de emisión
    jti: str = Field(default_factory=lambda: str(uuid4()))  # ID del token
    client_id: str  # ID del cliente OAuth
    scopes: t.List[PermissionsEnum] = []  # Permisos del Token
    tenant_id: t.Optional[str] = None  # ID del tenant (si aplica)
