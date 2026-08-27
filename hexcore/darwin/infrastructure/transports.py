"""
Los dos transportes de sesión: cookie `HttpOnly` para web, Bearer para nativo.

Una abstracción y dos adaptadores, resueltos **una vez** por request. El resto del código no
vuelve a ramificar: pide el transporte y lo usa.

El punto que no es obvio: **el transporte va atado al token**. Cada uno emite tokens con un
`aud` distinto, y el verificador acepta sólo el `aud` que corresponde al transporte por el
que llegó. Sin eso, un token emitido para cookie se puede presentar como
`Authorization: Bearer` y esquiva `SameSite` y el chequeo anti-CSRF por completo — que es
justamente la protección que el camino de cookie tiene y el de Bearer no necesita.

Y **nunca se hace el fallback "no hay cookie, probemos el header"** sobre el mismo tipo de
token. Ese fallback es lo que convierte los dos transportes en uno con dos entradas.

Requiere el extra `[api]` (Starlette). El módulo no se importa desde el núcleo: sólo desde
el borde HTTP de Darwin.
"""
from __future__ import annotations

import abc
import typing as t

from hexcore.darwin.domain.context import Transport

if t.TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from hexcore.darwin.application.config import CookieConfig
    from hexcore.darwin.domain.value_objects import TokenPair

__all__ = [
    "TRANSPORT_HEADER",
    "AbstractTransport",
    "CookieTransport",
    "BearerTransport",
    "TransportResolver",
]

#: Header con el que un cliente fija el transporte explícitamente.
#:
#: Existe para el caso real de una app nativa que comparte el backend con la web: sin él, un
#: cliente que manda cookies *y* header no puede decir cuál quiere que se use, y la regla
#: implícita lo sorprende.
TRANSPORT_HEADER = "X-Darwin-Transport"

#: Prefijo del esquema de `Authorization`, comparado sin distinguir mayúsculas porque
#: RFC 7235 lo declara case-insensitive y hay clientes que mandan `bearer`.
_BEARER = "bearer"


class AbstractTransport(abc.ABC):
    """
    Cómo entra y sale un par de tokens por HTTP.

    `extract` devuelve `None` si no hay credencial —que no es un error: es un request
    anónimo, y el middleware tiene que poder distinguir "no vino nada" de "vino algo
    inválido"—.
    """

    #: El transporte que este adaptador representa. Va al `aud` del token.
    name: t.ClassVar[Transport]

    @abc.abstractmethod
    def extract_access(self, request: "Request") -> str | None:
        """El access token del request, o `None` si no vino."""
        raise NotImplementedError

    @abc.abstractmethod
    def extract_refresh(self, request: "Request") -> str | None:
        """El refresh token del request, o `None` si no vino."""
        raise NotImplementedError

    @abc.abstractmethod
    def emit(self, response: "Response", tokens: "TokenPair") -> None:
        """Escribe el par en la respuesta."""
        raise NotImplementedError

    @abc.abstractmethod
    def clear(self, response: "Response") -> None:
        """Borra la credencial de la respuesta. Para el sign-out."""
        raise NotImplementedError


class CookieTransport(AbstractTransport):
    """
    Sesión en cookies `HttpOnly`. El transporte de un cliente web.

    Los atributos los decide `CookieConfig`, cuyos defaults son los seguros: prefijo
    `__Host-`, `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/` y **sin** `Domain`.

    `HttpOnly` es lo que hace que un XSS no pueda leer el token; el prefijo `__Host-` es lo
    que hace que un subdominio comprometido no pueda **escribirlo**. Los dos ataques son
    distintos y hacen falta las dos defensas.

    **Los tokens no van en el cuerpo de la respuesta.** Un cliente web no tiene dónde
    guardarlos que sea más seguro que la cookie, así que devolverlos sólo agrega una copia
    que puede terminar en `localStorage` — que es exactamente lo que `HttpOnly` evita.
    """

    name: t.ClassVar[Transport] = "cookie"

    def __init__(self, config: "CookieConfig") -> None:
        self._config = config

    def extract_access(self, request: "Request") -> str | None:
        return request.cookies.get(self._config.name_for("access"))

    def extract_refresh(self, request: "Request") -> str | None:
        return request.cookies.get(self._config.name_for("refresh"))

    def emit(self, response: "Response", tokens: "TokenPair") -> None:
        cfg = self._config
        response.set_cookie(
            cfg.name_for("access"),
            tokens.access_token,
            max_age=tokens.expires_in,
            httponly=cfg.http_only,
            secure=cfg.secure,
            samesite=cfg.same_site,
            path=cfg.path,
        )
        if tokens.refresh_token is not None:
            # El refresh va a `Path=/` igual que el access, y no a la ruta de refresh: con
            # el prefijo `__Host-` el navegador **exige** `Path=/`, así que restringirlo
            # haría que la cookie se rechace entera.
            response.set_cookie(
                cfg.name_for("refresh"),
                tokens.refresh_token,
                httponly=cfg.http_only,
                secure=cfg.secure,
                samesite=cfg.same_site,
                path=cfg.path,
            )
        # Cualquier respuesta que setea cookies de sesión varía según la credencial que vino,
        # así que no se puede cachear compartida.
        _vary(response, "Cookie")

    def clear(self, response: "Response") -> None:
        cfg = self._config
        for tipo in ("access", "refresh", "csrf"):
            response.delete_cookie(
                cfg.name_for(t.cast(t.Any, tipo)),
                path=cfg.path,
                httponly=cfg.http_only,
                secure=cfg.secure,
                samesite=cfg.same_site,
            )


class BearerTransport(AbstractTransport):
    """
    Sesión en `Authorization: Bearer`. El transporte de una app nativa (Expo, React Native).

    Acá los tokens **sí** van en el cuerpo: un cliente nativo no tiene cookies y necesita
    guardarlos él (en el keychain del sistema, idealmente). Es la asimetría deliberada con
    `CookieTransport`, y la razón de que el mismo endpoint responda distinto según el
    transporte.

    No hay chequeo anti-CSRF en este camino y no hace falta: el cliente adjunta el header a
    propósito en cada petición, así que un origen ajeno no puede provocar una petición
    autenticada sin tener el token — que es la definición del ataque que CSRF describe.
    """

    name: t.ClassVar[Transport] = "bearer"

    #: Header por el que llega el refresh token. **No** se reusa `Authorization`: mandar los
    #: dos por el mismo header obligaría al cliente a elegir uno, y la ruta de refresh
    #: necesita poder recibir los dos (el access para identificar la sesión, el refresh para
    #: rotarla).
    refresh_header: t.ClassVar[str] = "X-Refresh-Token"

    def extract_access(self, request: "Request") -> str | None:
        crudo = request.headers.get("Authorization")
        if not crudo:
            return None
        partes = crudo.split(None, 1)
        if len(partes) != 2 or partes[0].lower() != _BEARER:
            return None
        token = partes[1].strip()
        return token or None

    def extract_refresh(self, request: "Request") -> str | None:
        return request.headers.get(self.refresh_header) or None

    def emit(self, response: "Response", tokens: "TokenPair") -> None:
        # El cuerpo lo arma el router: acá sólo se marca que la respuesta depende del header
        # de autorización, para que ningún cache la comparta entre clientes.
        _vary(response, "Authorization")

    def clear(self, response: "Response") -> None:
        # No hay nada que borrar del lado del servidor: el cliente descarta sus tokens. La
        # sesión se revoca en la base, que es lo que de verdad la cierra.
        return None


class TransportResolver:
    """
    Decide el transporte de un request, una sola vez.

    Regla, en orden:

    1. El header `X-Darwin-Transport`, si nombra un transporte conocido.
    2. `Authorization: Bearer`, si vino.
    3. La cookie de sesión, si vino.
    4. El default configurado.

    El Bearer gana sobre la cookie porque un cliente que manda el header lo está haciendo a
    propósito, mientras que la cookie el navegador la manda sola. Cuando llegan los dos —una
    webview nativa dentro de una sesión web— el header es la señal intencional.

    Uso::

        resolver = TransportResolver(cookies=config.cookies)
        transporte = resolver.resolve(request)
        token = transporte.extract_access(request)
    """

    def __init__(
        self,
        *,
        cookies: "CookieConfig",
        default: Transport = "cookie",
    ) -> None:
        self._cookie = CookieTransport(cookies)
        self._bearer = BearerTransport()
        self._default: Transport = default

    @property
    def cookie(self) -> CookieTransport:
        return self._cookie

    @property
    def bearer(self) -> BearerTransport:
        return self._bearer

    def for_name(self, name: Transport) -> AbstractTransport:
        """
        El adaptador de un transporte por nombre.

        `internal` y `worker` no tienen adaptador HTTP —no hay request— así que se cae a
        cookie: es el único que puede *emitir* algo, y llegar acá con esos nombres significa
        que alguien pidió emitir tokens fuera de un request.
        """
        return self._bearer if name == "bearer" else self._cookie

    def resolve(self, request: "Request") -> AbstractTransport:
        declarado = (request.headers.get(TRANSPORT_HEADER) or "").strip().lower()
        if declarado == "bearer":
            return self._bearer
        if declarado == "cookie":
            return self._cookie

        if self._bearer.extract_access(request) is not None:
            return self._bearer
        if self._cookie.extract_access(request) is not None:
            return self._cookie

        return self.for_name(self._default)


def _vary(response: "Response", valor: str) -> None:
    """
    Agrega un valor a `Vary` sin pisar lo que ya estaba.

    Pisarlo es un error de cacheo con consecuencias de seguridad: si una respuesta declara
    `Vary: Cookie` y se sobreescribe con `Vary: Authorization`, un cache compartido puede
    servirle a un usuario la respuesta de otro.
    """
    actual = response.headers.get("Vary")
    if not actual:
        response.headers["Vary"] = valor
        return
    partes = [p.strip() for p in actual.split(",") if p.strip()]
    if valor.lower() not in {p.lower() for p in partes}:
        response.headers["Vary"] = ", ".join([*partes, valor])
