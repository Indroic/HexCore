"""
TOTP (RFC 6238) y HOTP (RFC 4226), sobre `hmac` de la stdlib.

**No se agrega `pyotp` ni ninguna otra dependencia, y la decisión tiene una razón concreta.**
El algoritmo entero son treinta líneas: un contador de 8 bytes big-endian, un HMAC-SHA1, un
truncado dinámico y un módulo. No hay criptografía nueva que implementar —el HMAC lo hace la
stdlib— así que una dependencia acá no compra corrección, compra superficie de cadena de
suministro en el camino de autenticación. Es el mismo criterio con el que el códec del sobre
firmado usa `hmac` + `json` en vez de un JWT.

SHA-1 es correcto y **no es un desvío**: es lo que especifica la RFC 4226 y lo único que
implementan Google Authenticator, Authy y 1Password para la ruta por default. El uso de HMAC no
depende de la resistencia a colisiones de la función hash, que es lo único que SHA-1 tiene
roto. Emitir un `secret` con SHA-256 daría códigos que la app del usuario no puede generar.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import typing as t
from urllib.parse import quote, urlencode

__all__ = [
    "DEFAULT_DIGITS",
    "DEFAULT_STEP",
    "DEFAULT_WINDOW",
    "SECRET_BYTES",
    "generate_totp_secret",
    "hotp_code",
    "totp_code",
    "current_step",
    "verify_totp",
    "provisioning_uri",
]

#: 20 bytes = 160 bits, lo que recomienda la RFC 4226 §4 para el secreto compartido. En base32
#: son 32 caracteres, que es lo que espera cualquier app autenticadora.
SECRET_BYTES = 20

#: 6 dígitos. 8 es válido por la RFC pero ninguna app popular lo genera por default, y un
#: usuario que copia 8 dígitos de una app que muestra 6 no entra nunca.
DEFAULT_DIGITS = 6

#: 30 segundos por paso, el default universal.
DEFAULT_STEP = 30

#: Cuántos pasos hacia atrás y hacia adelante se aceptan.
#:
#: 1 y no 0, porque el reloj del teléfono deriva y el usuario tarda en tipear: con ventana 0,
#: un código copiado en el segundo 29 llega vencido y el flujo se vuelve inusable. 1 y no 3,
#: porque cada paso extra multiplica por dos el espacio que un atacante puede probar con un
#: solo código robado — y con la ventana de 1 el código robado sirve, como máximo, 90 segundos.
DEFAULT_WINDOW = 1


def generate_totp_secret() -> str:
    """
    Un secreto TOTP nuevo, en base32 sin relleno.

    Sin relleno (`=`) porque las apps autenticadoras lo rechazan o lo dejan pasar según cuál,
    y el que lo rechaza le muestra al usuario un QR que no escanea sin decir por qué.
    """
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _decodificar(secret: str) -> bytes:
    """
    El secreto a bytes, tolerando relleno faltante, minúsculas y espacios.

    Tolerante a propósito: el secreto lo puede haber tipeado a mano un usuario que no pudo
    escanear el QR, y las apps lo muestran en grupos de cuatro separados por espacios.
    """
    limpio = secret.strip().replace(" ", "").upper()
    relleno = "=" * (-len(limpio) % 8)
    return base64.b32decode(limpio + relleno, casefold=True)


def hotp_code(secret: str, counter: int, *, digits: int = DEFAULT_DIGITS) -> str:
    """
    Un código HOTP (RFC 4226) para un contador dado.

    El truncado dinámico es el de la especificación: el nibble bajo del último byte elige el
    offset de los 4 bytes a leer. Elegir un offset fijo daría códigos que la app del usuario no
    genera.
    """
    digest = hmac.new(
        _decodificar(secret), struct.pack(">Q", counter), hashlib.sha1
    ).digest()
    offset = digest[-1] & 0x0F
    truncado = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFF_FFFF
    return str(truncado % (10**digits)).zfill(digits)


def current_step(timestamp: float, *, step: int = DEFAULT_STEP) -> int:
    """El número de paso de un instante. Es lo que se persiste para detectar el replay."""
    return int(timestamp // step)


def totp_code(
    secret: str,
    timestamp: float,
    *,
    digits: int = DEFAULT_DIGITS,
    step: int = DEFAULT_STEP,
) -> str:
    """El código TOTP válido en ese instante."""
    return hotp_code(secret, current_step(timestamp, step=step), digits=digits)


def verify_totp(
    secret: str,
    code: str,
    timestamp: float,
    *,
    digits: int = DEFAULT_DIGITS,
    step: int = DEFAULT_STEP,
    window: int = DEFAULT_WINDOW,
    after_step: int | None = None,
) -> int | None:
    """
    Verifica un código y devuelve **el paso con el que matcheó**, o `None`.

    Devuelve el paso y no un booleano justamente para poder persistirlo: sin eso no hay forma
    de impedir el replay. Un código TOTP vale 30 segundos, así que quien lo lee por encima del
    hombro —o lo saca de un log, o de un formulario de phishing— lo puede usar de nuevo dentro
    de esa ventana. Guardando el último paso usado y pasándolo en `after_step`, el segundo
    intento se rechaza aunque el código siga siendo criptográficamente válido.

    Args:
        secret: El secreto en base32.
        code: Lo que tipeó el usuario. Se limpia de espacios; si no son dígitos, se rechaza sin
            calcular nada.
        timestamp: Epoch en segundos, del reloj **inyectado** — no `time.time()`.
        after_step: Si viene, se rechaza cualquier paso menor o igual. Es la defensa de replay.

    Returns: El paso con el que matcheó, o `None` si ninguno.
    """
    limpio = code.strip().replace(" ", "").replace("-", "")
    if len(limpio) != digits or not limpio.isdigit():
        return None

    ahora = current_step(timestamp, step=step)

    # Se recorre entero y se acumula el resultado en vez de cortar al primer match: salir
    # temprano hace que el tiempo de respuesta diga **qué paso** acertó el atacante, o sea
    # cuánto deriva el reloj del usuario. Es poca información, pero es gratis no darla.
    encontrado: int | None = None
    for candidato in range(ahora - window, ahora + window + 1):
        esperado = hotp_code(secret, candidato, digits=digits)
        if hmac.compare_digest(esperado, limpio) and encontrado is None:
            encontrado = candidato

    if encontrado is None:
        return None
    if after_step is not None and encontrado <= after_step:
        # Criptográficamente válido, pero ya se usó. Ver el docstring.
        return None
    return encontrado


def provisioning_uri(
    secret: str,
    *,
    account: str,
    issuer: str,
    digits: int = DEFAULT_DIGITS,
    step: int = DEFAULT_STEP,
) -> str:
    """
    La URI `otpauth://` para el QR.

    El `issuer` va **dos veces** —en la etiqueta y como parámetro— y eso no es redundancia: la
    etiqueta es lo que muestran las apps viejas y el parámetro lo que leen las nuevas.
    Poniendo sólo el parámetro, un usuario con tres cuentas ve tres entradas idénticas.

    Uso::

        from hexcore.darwin.plugins.two_factor.totp import (
            generate_totp_secret,
            provisioning_uri,
        )

        secreto = generate_totp_secret()
        uri = provisioning_uri(secreto, account="ana@ejemplo.com", issuer="Mi App")
        assert uri.startswith("otpauth://totp/Mi%20App:ana%40ejemplo.com?")
    """
    etiqueta = quote(f"{issuer}:{account}", safe="")
    parametros: dict[str, t.Any] = {
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": digits,
        "period": step,
    }
    return f"otpauth://totp/{etiqueta}?{urlencode(parametros)}"
