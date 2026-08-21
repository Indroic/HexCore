"""
Hasheo de contraseñas y de tokens.

Dos primitivas distintas, y la diferencia importa:

- **Contraseñas → Argon2id.** Entropía baja, elegida por un humano, así que hay que
  defenderse de un ataque de diccionario con hardware dedicado. Argon2id es el ganador del
  Password Hashing Competition y la recomendación de OWASP.
- **Tokens de sesión y códigos → SHA-256.** Son aleatorios de 256 bits: no hay diccionario
  del que defenderse, así que un KDF lento no compra seguridad y sí cuesta latencia en el
  camino caliente de cada petición autenticada.

Usar Argon2 para el token de sesión sería tres órdenes de magnitud más lento por request sin
ganar nada. Usar SHA-256 para la contraseña sería regalar la base de credenciales al primer
dump. Son errores simétricos y los dos son fáciles de cometer.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import typing as t

from hexcore.darwin.domain.ports import AbstractPasswordHasher

if t.TYPE_CHECKING:
    from argon2 import PasswordHasher as Argon2Hasher

__all__ = [
    "Argon2PasswordHasher",
    "hash_token",
    "compare_hashes",
    "generate_token",
    "generate_numeric_code",
    "derive_csrf_token",
]

#: Separación de dominio del HMAC del valor anti-CSRF. Sin la etiqueta, el mismo secreto
#: firmando otro protocolo permitiría reusar una firma de allá como valor válido acá.
_CSRF_LABEL = b"hexcore.darwin.csrf.v1"


def derive_csrf_token(session_id: str, secret: str) -> str:
    """
    Deriva el valor anti-CSRF de una sesión: `HMAC(secreto, sid)`.

    **Derivado y no aleatorio, y esa es toda la decisión.** La cookie de CSRF no puede llevar
    el prefijo `__Host-` ni `HttpOnly` —el cliente tiene que poder leerla para devolverla en
    el header— así que un subdominio comprometido **puede escribirla**. Con un valor
    aleatorio, el atacante escribe la cookie y manda el mismo valor en el header: pasa el
    double-submit con un valor que eligió él. Derivándolo del `sid` con una clave del
    servidor, un valor inventado no verifica.

    Es determinista a propósito: el mismo `sid` da el mismo valor, así que el cliente puede
    perder la cookie y recuperarla sin rotar la sesión.
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        _CSRF_LABEL + b"." + session_id.encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()

class Argon2PasswordHasher(AbstractPasswordHasher):
    """
    `AbstractPasswordHasher` con Argon2id, más verificación de hashes bcrypt legados.

    Los parámetros por defecto los elige `argon2-cffi`, que los mantiene alineados con la
    recomendación vigente del RFC 9106. **No se hardcodean acá**: fijarlos en el código
    significa quedarse con los valores de hoy para siempre, y el costo recomendado sube con
    el hardware. Si necesitás ajustarlos —por ejemplo para un entorno con poca RAM— pasá tu
    propio `argon2.PasswordHasher`.

    Uso::

        hasher = Argon2PasswordHasher()
        guardado = hasher.hash("correohorsebatterystaple")

        if hasher.verify("correohorsebatterystaple", guardado):
            if hasher.needs_rehash(guardado):
                guardado = hasher.hash("correohorsebatterystaple")   # subí el costo
    """

    def __init__(self, hasher: "Argon2Hasher | None" = None) -> None:
        if hasher is None:
            from argon2 import PasswordHasher

            hasher = PasswordHasher()
        self._hasher = hasher

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, hashed: str) -> bool:
        """
        Si la contraseña coincide. **No lanza nunca**: devuelve `False`.

        Que no lance es deliberado. `argon2` distingue "no coincide" de "el hash está
        corrupto" con excepciones distintas, y propagarlas le daría al atacante un canal
        para distinguir esos casos — además de convertir un hash mal migrado en un 500 en
        vez de un login fallido.

        Reconoce hashes de bcrypt para poder migrar sin forzar un reseteo masivo de
        contraseñas: se verifica con el algoritmo viejo y `needs_rehash` pide regenerarlo.
        """
        from argon2.exceptions import Argon2Error

        if not hashed:
            return False

        if hashed.startswith(("$2a$", "$2b$", "$2y$")):
            return self._verificar_bcrypt(password, hashed)

        try:
            return bool(self._hasher.verify(hashed, password))
        except (Argon2Error, ValueError, TypeError):
            return False

    def needs_rehash(self, hashed: str) -> bool:
        """
        Si conviene regenerar el hash al próximo login exitoso.

        Devuelve `True` para todo hash que no sea Argon2: es la señal de migración. Sin esto,
        una base migrada desde bcrypt se queda en bcrypt para siempre, porque nadie vuelve a
        pasar por el camino donde se podría actualizar.
        """
        if not hashed:
            return True
        if hashed.startswith(("$2a$", "$2b$", "$2y$")):
            return True
        try:
            return bool(self._hasher.check_needs_rehash(hashed))
        except (ValueError, TypeError):
            return True

    @staticmethod
    def _verificar_bcrypt(password: str, hashed: str) -> bool:
        """
        Verifica un hash bcrypt legado, si `bcrypt` está instalado.

        Es opcional a propósito: sólo lo necesita quien migra desde una base existente, y no
        tiene sentido imponerle la dependencia a todo el mundo. Sin `bcrypt` instalado
        devuelve `False`, o sea que el login falla — que es el lado seguro del error.
        """
        import importlib

        try:
            # Por `importlib` y no con un `import bcrypt`: el paquete no está en ningún extra
            # —sólo lo necesita quien migra desde una base existente— así que el checker no lo
            # puede resolver, y un `import` directo dejaría el módulo entero como no resuelto.
            # Así el tipo es `Any` explícito y declarado.
            bcrypt: t.Any = importlib.import_module("bcrypt")
        except ImportError:
            return False
        try:
            return bool(bcrypt.checkpw(password.encode(), hashed.encode()))
        except (ValueError, TypeError):
            return False


# ── Tokens y códigos ──────────────────────────────────────────────────────────
def hash_token(token: str) -> str:
    """
    SHA-256 hexadecimal de un token. Lo que se guarda en `session.token_hash`.

    SHA-256 y no Argon2 porque el token es aleatorio de 256 bits: no hay entropía baja que
    proteger, así que un KDF lento cuesta latencia en cada verificación de sesión y no compra
    resistencia a nada.

    Lo que sí compra el hash: un dump de la tabla `session` deja de ser un set de credenciales
    utilizables. Better Auth guarda el token en claro; acá no.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compare_hashes(a: str, b: str) -> bool:
    """
    Compara dos hashes en **tiempo constante**.

    `==` sobre strings sale en el primer byte distinto, así que el tiempo de respuesta filtra
    cuántos caracteres del prefijo acertó el atacante. Con un token de sesión o un OTP, eso
    convierte una búsqueda exponencial en una lineal.
    """
    return hmac.compare_digest(a, b)


def generate_token(nbytes: int = 32) -> str:
    """
    Un token de sesión aleatorio, URL-safe.

    32 bytes = 256 bits. `secrets` y no `random`: `random` usa Mersenne Twister, que es
    predecible una vez que observás suficiente salida, y para un token de sesión eso es una
    toma de cuenta.
    """
    return secrets.token_urlsafe(nbytes)


def generate_numeric_code(digits: int = 6) -> str:
    """
    Un código numérico para OTP o verificación por SMS.

    `secrets.randbelow` y no `randint` sobre un rango: el sesgo del módulo sobre un espacio
    de 10^6 es chico pero real, y acá el espacio ya es chico de entrada. Por eso el código
    **necesita** techo de intentos (`Verification.attempts`): 10^6 combinaciones se agotan en
    minutos si nadie las cuenta.
    """
    if digits < 4:
        raise ValueError(
            "generate_numeric_code(digits=...) tiene que ser >= 4. Menos que eso es un "
            "espacio de búsqueda que se agota a mano."
        )
    limite = 10**digits
    return str(secrets.randbelow(limite)).zfill(digits)
