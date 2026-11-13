"""
IP y navegador del cliente de una petición, tal como los ve Darwin.

Un solo lugar para una decisión que antes estaba copiada en cada router (`_ip` en el de
identidad y en los de `oauth`, `passkey`, `magic_link` e `impersonate`): de dónde sale la IP.

**La IP sale del socket, salvo que se declare un proxy de confianza.** `X-Forwarded-For` lo
escribe quien hace la petición, así que honrarlo sin condiciones deja que cualquiera elija la IP
que queda en la sesión, en los eventos y en el cubo del límite de intentos. Con
`IdentityConfig.trusted_proxies` vacío —el default— la cabecera se ignora. Con proxies
declarados, sólo se lee si el **par TCP inmediato** es uno de ellos, y se toma la **última IP que
no sea de confianza** recorriendo la cabecera de derecha a izquierda: la derecha es lo único que
agregaron nuestros proxies; lo que está más a la izquierda lo puede inventar el cliente.

Las funciones de acá no dependen de FastAPI: aceptan cualquier objeto con `.client` y `.headers`
(un `starlette.requests.Request`, un `WebSocket`), así que un servicio que recibe el transporte
también las puede usar.
"""
from __future__ import annotations

import ipaddress
import typing as t
from dataclasses import dataclass
from functools import lru_cache

__all__ = [
    "ClientInfo",
    "MAX_USER_AGENT_LENGTH",
    "client_info_from_request",
    "client_info_of",
    "identity_client_ip_key",
    "parse_trusted_proxies",
    "resolve_client_ip",
]

#: El tope de la columna `user_agent` de la sesión. Un cliente hostil puede mandar un
#: `User-Agent` enorme; lo que va a una fila, a un evento o a una alerta no necesita más.
MAX_USER_AGENT_LENGTH = 1024

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class _HasClient(t.Protocol):
    @property
    def client(self) -> t.Any: ...

    @property
    def headers(self) -> t.Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class ClientInfo:
    """Lo que se sabe del cliente de una petición. Cualquiera de los dos puede faltar."""

    ip: str | None = None
    user_agent: str | None = None

    def as_kwargs(self) -> dict[str, str | None]:
        """Para desempaquetar en un servicio: `ip_address=` y `user_agent=`."""
        return {"ip_address": self.ip, "user_agent": self.user_agent}


@lru_cache(maxsize=32)
def _parsear_redes(entries: tuple[str, ...]) -> tuple[_Network, ...]:
    redes: list[_Network] = []
    for entrada in entries:
        try:
            redes.append(ipaddress.ip_network(entrada.strip(), strict=False))
        except ValueError as exc:
            raise ValueError(
                f"IdentityConfig.trusted_proxies: {entrada!r} no es una IP ni una red CIDR "
                f"válida. Ejemplos válidos: '10.0.0.0/8', '172.18.0.5', 'fd00::/8'."
            ) from exc
    return tuple(redes)


def parse_trusted_proxies(entries: t.Iterable[str]) -> tuple[_Network, ...]:
    """
    Convierte la lista de `trusted_proxies` en redes. Cacheado: se llama en cada petición.

    Acepta IPs sueltas (`"172.18.0.5"`) y redes CIDR (`"10.0.0.0/8"`).

    Raises:
        ValueError: si alguna entrada no es una IP ni una red válida.
    """
    return _parsear_redes(tuple(entries))


def _parsear(direccion: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(direccion)
    except ValueError:
        return None


def _es_de_confianza(
    direccion: ipaddress.IPv4Address | ipaddress.IPv6Address, redes: t.Sequence[_Network]
) -> bool:
    # `in` entre versiones distintas (una v4 contra una red v6) devuelve `False` y no lanza.
    return any(direccion in red for red in redes)


def resolve_client_ip(
    request: _HasClient, trusted_proxies: t.Iterable[str] = ()
) -> str | None:
    """
    La IP real del cliente.

    Sin `trusted_proxies`, o si el par inmediato no es uno de ellos, es `request.client.host` y
    **se ignora** `X-Forwarded-For`. Con el par en la lista, es la última IP de la cabecera que
    no sea de confianza (de derecha a izquierda).

    Si la cabecera falta, está mal formada o es toda de proxies, devuelve la IP del par: ante la
    duda se cae al valor que no controla el cliente.
    """
    cliente = request.client
    par: str | None = cliente.host if cliente else None

    redes = parse_trusted_proxies(trusted_proxies)
    if not redes or par is None:
        return par

    par_ip = _parsear(par)
    if par_ip is None or not _es_de_confianza(par_ip, redes):
        return par

    reenviado = request.headers.get("X-Forwarded-For")
    if not reenviado:
        return par

    saltos = [s.strip() for s in reenviado.split(",") if s.strip()]
    for salto in reversed(saltos):
        ip = _parsear(salto)
        if ip is None:
            # Basura en la cabecera: lo de más a la izquierda ya no es confiable.
            return par
        if not _es_de_confianza(ip, redes):
            return str(ip)
    return par


def client_info_from_request(
    request: _HasClient, trusted_proxies: t.Iterable[str] = ()
) -> ClientInfo:
    """La IP (ver `resolve_client_ip`) y el `User-Agent`, truncado al largo de la columna."""
    agente = (request.headers.get("User-Agent") or "").strip()
    return ClientInfo(
        ip=resolve_client_ip(request, trusted_proxies),
        user_agent=agente[:MAX_USER_AGENT_LENGTH] or None,
    )


def _proxies_configurados() -> tuple[str, ...]:
    # Import perezoso: el contenedor es de la capa de aplicación y sólo existe una vez que la app
    # llamó a `configure_identity`, no al importar este módulo.
    from hexcore.darwin.application.container import get_identity_container

    return get_identity_container().config.trusted_proxies


def client_info_of(request: _HasClient) -> ClientInfo:
    """
    El `ClientInfo` de una petición HTTP, con los `trusted_proxies` de la configuración activa.

    Es lo que usan todos los routers de Darwin para pasar `ip_address` y `user_agent` a los
    servicios, así que la sesión, los eventos y el límite de intentos dicen siempre lo mismo.
    """
    return client_info_from_request(request, _proxies_configurados())


def identity_client_ip_key(request: t.Any) -> str:
    """
    Clave de `rate_limit` por la **misma IP** que lleva la sesión y los eventos.

    Reemplaza a `client_ip_key` en las rutas de Darwin: sin `trusted_proxies` da lo mismo
    (`request.client.host`), y con ellos deja de ser un cubo global detrás de un proxy.
    """
    return resolve_client_ip(request, _proxies_configurados()) or "unknown"
