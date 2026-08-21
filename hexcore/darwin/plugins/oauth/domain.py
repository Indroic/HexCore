"""
El dominio de `oauth`: PKCE, el puerto HTTP, las excepciones y sus status.

Nada de esto toca sqlalchemy ni httpx: lo importa el borde HTTP para mapear los status, y tiene
que poder importarse en un proceso sin los extras.
"""
from __future__ import annotations

import abc
import base64
import hashlib
import secrets
import typing as t
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from hexcore.darwin.domain.exceptions import (
    AuthenticationError,
    AuthorizationError,
    IdentityError,
)

__all__ = [
    "OAuthTokens",
    "OAuthState",
    "AbstractOAuthHttpClient",
    "AbstractOAuthStateRepository",
    "OAuthError",
    "OAuthProviderNotConfiguredError",
    "OAuthStateError",
    "OAuthExchangeError",
    "OAuthAccountNotLinkedError",
    "OAuthAccountAlreadyLinkedError",
    "OAuthEmailNotVerifiedError",
    "OAUTH_EXCEPTION_STATUS_MAP",
    "generate_pkce_verifier",
    "pkce_challenge",
    "CODE_CHALLENGE_METHOD",
]

#: S256 y **nunca** `plain`. `plain` manda el verificador en la URL de autorización, o sea que
#: cualquiera que vea esa URL —el historial del navegador, un log de proxy, un `Referer`— puede
#: canjear el código. La RFC 7636 permite `plain` sólo para clientes que no pueden hacer SHA-256,
#: que en Python no existen.
CODE_CHALLENGE_METHOD = "S256"

#: 64 bytes de aleatoriedad. La RFC 7636 §4.1 pide un verificador de 43 a 128 caracteres; 64
#: bytes en base64url sin relleno dan 86, holgadamente dentro del rango y con margen de sobra
#: sobre el mínimo.
_VERIFIER_BYTES = 64


def generate_pkce_verifier() -> str:
    """El `code_verifier` de PKCE: base64url sin relleno, como pide la RFC 7636."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_VERIFIER_BYTES)).decode(
        "ascii"
    ).rstrip("=")


def pkce_challenge(verifier: str) -> str:
    """
    El `code_challenge` correspondiente: base64url(SHA256(verifier)), sin relleno.

    El relleno se saca porque la RFC lo especifica así y algunos proveedores rechazan el `=`
    aunque vaya escapado — con un mensaje que no dice cuál es el problema.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class OAuthTokens(t.NamedTuple):
    """Lo que devuelve el proveedor al canjear el código."""

    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    expires_in: int | None = None
    refresh_expires_in: int | None = None
    scope: str | None = None


# ── El puerto HTTP ────────────────────────────────────────────────────────────
class AbstractOAuthHttpClient(abc.ABC):
    """
    Las dos llamadas de red que el flujo necesita.

    Es un puerto y no `httpx` directo por dos razones, y la segunda es la que importa: `httpx` no
    es dependencia del extra `[darwin]` —va en `[darwin-oauth]`— y un test del flujo no debería
    necesitar red. Con el puerto, el test inyecta un doble y ejercita el intercambio completo sin
    levantar un servidor.
    """

    @abc.abstractmethod
    async def exchange_code(
        self,
        token_url: str,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: str,
    ) -> OAuthTokens:
        """
        Canjea el código por tokens.

        Raises:
            OAuthExchangeError: el proveedor rechazó el canje o respondió algo inesperado.
        """

    @abc.abstractmethod
    async def fetch_profile(
        self, userinfo_url: str, *, access_token: str
    ) -> dict[str, t.Any]:
        """
        Trae el `userinfo` crudo.

        Raises:
            OAuthExchangeError: el proveedor rechazó la llamada.
        """


# ── El `state` ────────────────────────────────────────────────────────────────
class OAuthState(BaseModel):
    """
    Un flujo de autorización en vuelo.

    Vive segundos o minutos: se crea al redirigir al proveedor y se consume en el callback. Ver
    el mixin (`models_mixins.py`) para qué guarda cada campo y por qué el `code_verifier` no
    puede viajar en la URL.
    """

    id: UUID
    provider_id: str
    state_hash: str
    code_verifier_encrypted: str
    redirect_uri: str
    link_user_id: UUID | None
    expires_at: datetime
    consumed_at: datetime | None = None


class AbstractOAuthStateRepository(abc.ABC):
    """
    Guardar y canjear el `state`.

    Vive en el dominio y no en `repository.py` porque el servicio lo necesita, y `repository.py`
    importa sqlalchemy en el nivel superior: dejarlo ahí haría que importar el servicio exija el
    extra `[sql]`, y hay un test que lo verifica.
    """

    @abc.abstractmethod
    async def add(self, state: OAuthState) -> OAuthState:
        """Guarda el flujo en vuelo."""

    @abc.abstractmethod
    async def consume(
        self, provider_id: str, state_hash: str, *, at: datetime
    ) -> OAuthState | None:
        """
        Canjea el `state` y lo marca consumido, en **una sola sentencia**.

        Filtra por `provider_id` además del hash: un `state` emitido para Google no se puede
        canjear en el callback de GitHub. `None` si no existe, venció o ya se usó — **un solo
        valor para los tres**, porque distinguirlos le diría a quien prueba si hay un flujo en
        curso.
        """

    @abc.abstractmethod
    async def delete_expired(self, *, before: datetime) -> int:
        """Barre los que quedaron. El usuario que cierra la pestaña deja uno cada vez."""


# ── Las excepciones ───────────────────────────────────────────────────────────
class OAuthError(IdentityError):
    """Base de las fallas del plugin."""


class OAuthProviderNotConfiguredError(OAuthError):
    """
    Se pidió un proveedor que no está configurado. 404.

    404 y no 400: desde afuera, un proveedor no configurado y una ruta que no existe son lo
    mismo, y un 400 con "proveedor desconocido" le confirmaría a quien enumera cuáles sí están.
    """


class OAuthStateError(AuthenticationError):
    """
    El `state` no existe, venció, ya se usó o no coincide.

    Es **el** mecanismo anti-CSRF del flujo: sin él, un atacante hace que la víctima complete un
    callback con un `code` del atacante y le vincula su cuenta del proveedor. Un solo error para
    los cuatro casos: distinguirlos le diría a quien prueba si hay un flujo en curso.
    """


class OAuthExchangeError(OAuthError):
    """
    El proveedor rechazó el canje, o respondió algo que no se puede interpretar. 502.

    502 y no 500: la falla es de un servicio *aguas arriba*, y confundirla con un error propio
    manda a buscar el bug al lugar equivocado.
    """


class OAuthAccountNotLinkedError(OAuthError):
    """
    Existe una cuenta local con ese mail, pero la identidad del proveedor no está vinculada.

    ⚠️ **Es la excepción que evita la toma de cuentas más común de OAuth.** Vincular
    automáticamente por coincidencia de mail deja que cualquiera se registre en un IdP permisivo
    con el mail de la víctima y entre a su cuenta. La única vinculación segura es la explícita:
    el usuario inicia sesión con su método actual y *desde ahí* vincula el proveedor.

    409: hay un conflicto que el usuario puede resolver, y el mensaje le dice cómo.
    """


class OAuthAccountAlreadyLinkedError(OAuthError):
    """
    Esa identidad del proveedor ya está vinculada a otra cuenta local. 409.

    Se rechaza en vez de mover la vinculación: moverla dejaría a la primera cuenta sin su método
    de acceso, y si era el único, sin acceso.
    """


class OAuthEmailNotVerifiedError(AuthorizationError):
    """
    El proveedor no informa el mail como verificado y la política exige que lo esté. 403.

    Aplica cuando se habilitó la vinculación automática por mail: sin la verificación del
    proveedor, esa vinculación es exactamente el agujero que `OAuthAccountNotLinkedError`
    describe.
    """


#: El mapa que el plugin aporta vía `exception_status_map()`.
OAUTH_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    OAuthProviderNotConfiguredError: 404,
    OAuthStateError: 401,
    OAuthExchangeError: 502,
    OAuthAccountNotLinkedError: 409,
    OAuthAccountAlreadyLinkedError: 409,
    OAuthEmailNotVerifiedError: 403,
    # `OAuthError` (la base) **no** se mapea, por lo mismo que el núcleo no mapea
    # `IdentityError`: `_specificity` ordena por profundidad de MRO, así que mapearla haría que
    # una falla nueva se tragara con ese status en vez de aparecer como un 500 en los tests.
}
