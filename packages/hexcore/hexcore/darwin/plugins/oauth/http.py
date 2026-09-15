"""
El cliente HTTP del flujo OAuth, sobre `httpx`. Requiere el extra `[darwin-oauth]`.

Dos llamadas y cuatro decisiones:

1. **`Accept: application/json` en el canje.** GitHub responde
   `application/x-www-form-urlencoded` si no se lo pide, y parsear eso a mano es la clase de
   código que se rompe con el primer campo que trae un `&`.
2. **Timeout explícito y corto.** Sin timeout, `httpx` espera indefinidamente: un proveedor
   colgado deja pedidos ocupando workers, y el usuario mirando una pantalla en blanco. 10
   segundos es de sobra para dos llamadas a un endpoint de OAuth.
3. **`follow_redirects=False`.** Un `token_url` que redirige a otro host mandaría el
   `client_secret` a donde diga el proveedor comprometido.
4. **El cuerpo de error del proveedor no se propaga al usuario.** Se loguea y se responde el
   mensaje genérico: la respuesta de un proveedor puede traer el `client_id`, un fragmento del
   secreto en un mensaje de "credenciales inválidas", o el detalle de la cuenta.
"""
from __future__ import annotations

import logging
import typing as t

from hexcore.darwin.plugins.oauth.domain import (
    AbstractOAuthHttpClient,
    OAuthExchangeError,
    OAuthTokens,
)

__all__ = ["HttpxOAuthClient", "DEFAULT_TIMEOUT"]

logger = logging.getLogger("hexcore.darwin.oauth")

#: 10 segundos. Ver el punto 2 del docstring del módulo.
DEFAULT_TIMEOUT = 10.0


class HttpxOAuthClient(AbstractOAuthHttpClient):
    """
    `AbstractOAuthHttpClient` sobre `httpx`.

    Args:
        timeout: Segundos.
        client: Un `httpx.AsyncClient` propio, para reusar un pool de conexiones o para pasarle
            un `transport` de test. Si viene, **no se cierra acá**: lo cierra quien lo creó.

    Uso::

        from hexcore.darwin.plugins.oauth.http import HttpxOAuthClient

        cliente = HttpxOAuthClient()
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, client: t.Any = None) -> None:
        self._timeout = timeout
        self._client = client

    def _abrir(self) -> t.Any:
        """
        Un cliente por operación si no se inyectó uno.

        Uno por operación y no uno de larga vida guardado en el plugin: el plugin se instancia
        antes del loop de eventos —al declarar el registro— y un `AsyncClient` creado ahí queda
        atado a un loop que puede no ser el que corre.
        """
        if self._client is not None:
            return _NoCerrar(self._client)

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise ImportError(
                "El plugin 'oauth' necesita httpx. Instalalo con:\n\n"
                "    pip install 'hexcore[darwin-oauth]'"
            ) from exc

        return httpx.AsyncClient(
            timeout=self._timeout,
            # Ver el punto 3 del docstring del módulo: un redirect del `token_url` mandaría el
            # `client_secret` a donde diga el proveedor.
            follow_redirects=False,
        )

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
        datos = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        }

        async with self._abrir() as cliente:
            respuesta = await cliente.post(
                token_url,
                data=datos,
                headers={"Accept": "application/json"},
            )

        if respuesta.status_code >= 400:
            # El cuerpo va al log y **no** a la respuesta: puede traer el `client_id` o un
            # fragmento del secreto en un mensaje de "credenciales inválidas".
            logger.warning(
                "El proveedor rechazó el canje de código (%s): %s",
                respuesta.status_code,
                respuesta.text[:500],
            )
            raise OAuthExchangeError(
                "El proveedor rechazó el intercambio del código de autorización."
            )

        try:
            crudo = respuesta.json()
        except Exception as exc:
            raise OAuthExchangeError(
                "El proveedor respondió algo que no es JSON en el canje del código."
            ) from exc

        if not isinstance(crudo, dict):
            raise OAuthExchangeError(
                "El proveedor no devolvió un objeto JSON en el canje del código."
            )

        cuerpo = t.cast("dict[str, t.Any]", crudo)
        if not cuerpo.get("access_token"):
            # Algunos proveedores devuelven 200 con `{"error": "bad_verification_code"}`. Sin
            # este chequeo, el flujo seguiría con un access token vacío y fallaría más adelante,
            # lejos de la causa.
            logger.warning("El canje devolvió 200 sin access_token: %s", cuerpo)
            raise OAuthExchangeError(
                "El proveedor no devolvió un access token en el canje del código."
            )

        return OAuthTokens(
            access_token=str(cuerpo["access_token"]),
            refresh_token=_opcional(cuerpo.get("refresh_token")),
            id_token=_opcional(cuerpo.get("id_token")),
            expires_in=_entero(cuerpo.get("expires_in")),
            refresh_expires_in=_entero(cuerpo.get("refresh_token_expires_in")),
            scope=_opcional(cuerpo.get("scope")),
        )

    async def fetch_profile(
        self, userinfo_url: str, *, access_token: str
    ) -> dict[str, t.Any]:
        async with self._abrir() as cliente:
            respuesta = await cliente.get(
                userinfo_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )

        if respuesta.status_code >= 400:
            logger.warning(
                "El proveedor rechazó la lectura del perfil (%s): %s",
                respuesta.status_code,
                respuesta.text[:500],
            )
            raise OAuthExchangeError("El proveedor rechazó la lectura del perfil.")

        try:
            crudo = respuesta.json()
        except Exception as exc:
            raise OAuthExchangeError(
                "El proveedor respondió algo que no es JSON en el perfil."
            ) from exc

        if not isinstance(crudo, dict):
            raise OAuthExchangeError("El perfil del proveedor no es un objeto JSON.")
        return t.cast("dict[str, t.Any]", crudo)


class _NoCerrar:
    """
    Envuelve un cliente inyectado para que el `async with` no lo cierre.

    Cerrar un cliente que nos prestaron dejaría inutilizable el pool de quien lo creó, y el
    síntoma —"el segundo login falla"— no señala a este archivo.
    """

    __slots__ = ("_cliente",)

    def __init__(self, cliente: t.Any) -> None:
        self._cliente = cliente

    async def __aenter__(self) -> t.Any:
        return self._cliente

    async def __aexit__(self, *_: t.Any) -> None:
        return None


def _opcional(valor: t.Any) -> str | None:
    """Un string no vacío, o `None`. Los proveedores mandan `""` donde corresponde `null`."""
    return str(valor) if valor else None


def _entero(valor: t.Any) -> int | None:
    """
    Un entero, o `None` si no se puede interpretar.

    Se tolera basura porque el campo es informativo —de él sale `expires_at`— y un `expires_in`
    raro no debería hacer fallar un login que el proveedor ya autorizó.
    """
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None
