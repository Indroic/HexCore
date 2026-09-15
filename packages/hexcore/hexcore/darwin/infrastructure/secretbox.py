"""
Cifrado autenticado de secretos en reposo, con clave derivada de `secret_key`.

**Cuándo se usa esto y cuándo un hash.** El resto de Darwin guarda hashes: el token de sesión, el
refresh, el magic link. Un hash alcanza cuando lo único que hay que hacer es *comparar*. No
alcanza cuando el valor tiene que **recuperarse**: un secreto TOTP hay que recalcularlo para
verificar un código, y un `refresh_token` de Google hay que mandárselo a Google. Para esos dos
casos, cifrar es la única opción — y guardarlos en claro convierte un dump de la base en
credenciales utilizables, propias y de terceros.

Se usa **JWE compacto `dir` + `A256GCM` de `joserfc`**, que ya es dependencia del extra `[darwin]`
porque firma los tokens. Es AEAD, así que el texto cifrado está **autenticado**: alguien con
acceso de escritura a la base no puede sustituir el secreto de un usuario por uno que él conoce
sin que el descifrado falle. Un XOR con una clave derivada, o cualquier cifrado sin MAC, dejaría
esa puerta abierta. Y no se agrega `cryptography` como dependencia nueva para algo que la que ya
está resuelve.

**Cada propósito tiene su etiqueta y por lo tanto su clave.** La clave sale de un HMAC de
`secret_key` con la etiqueta, así que la que cifra secretos TOTP no es la que cifra tokens de
OAuth ni la que firma sobres ni la que deriva valores anti-CSRF. Reusar el mismo material para
todo hace que romper uno rompa todos, y una etiqueta es gratis.
"""
from __future__ import annotations

import hashlib
import hmac
import typing as t

__all__ = ["SecretBox", "SecretDecryptionError"]

#: La cabecera del JWE. `dir` significa "la clave de contenido es la que doy", que es lo correcto
#: acá: no hay un segundo destinatario al que envolverle una clave.
_CABECERA: dict[str, t.Any] = {"alg": "dir", "enc": "A256GCM"}


class SecretDecryptionError(Exception):
    """
    No se pudo descifrar.

    Pasa en dos casos, y los dos son operativos y no del usuario: cambió `secret_key` sin migrar
    las filas, o alguien alteró la columna. Se distingue de "el valor es incorrecto" a propósito:
    decirle "código inválido" a un usuario cuya fila no se puede descifrar lo deja reintentando
    para siempre contra un problema que él no puede resolver.
    """


class SecretBox:
    """
    Cifra y descifra con una clave derivada de `secret_key` y una etiqueta de propósito.

    Args:
        secret_key: La clave de firma del despliegue.
        label: Qué se cifra con esta caja. **Cambiarla invalida todo lo cifrado antes**, así que
            va versionada (``...v1``) y es una constante del módulo que la usa, no un parámetro
            de configuración.

    Uso::

        caja = SecretBox("una-clave-de-firma-larga", label=b"mi.proposito.v1")
        guardado = caja.encrypt("el secreto")
        assert caja.decrypt(guardado) == "el secreto"
    """

    __slots__ = ("_clave",)

    def __init__(self, secret_key: str, *, label: bytes) -> None:
        from joserfc.jwk import OctKey

        # HKDF-Extract/Expand reducido a un solo HMAC: la entrada ya tiene entropía de sobra
        # —`secret_key` se valida con un mínimo de largo— así que la expansión iterada de HKDF
        # no agrega nada. Lo que sí importa es que la etiqueta separe este propósito de los
        # demás.
        material = hmac.new(secret_key.encode("utf-8"), label, hashlib.sha256).digest()
        self._clave = OctKey.import_key(material)

    def encrypt(self, plaintext: str) -> str:
        """
        El valor como JWE compacto.

        Cada llamada da un texto distinto (nonce nuevo). Es lo que evita que dos filas con el
        mismo valor se vean iguales — que con tokens de OAuth diría qué usuarios comparten una
        cuenta de servicio.
        """
        from joserfc import jwe

        return jwe.encrypt_compact(_CABECERA, plaintext.encode("utf-8"), self._clave)

    def decrypt(self, ciphertext: str) -> str:
        """
        El valor en claro.

        Raises:
            SecretDecryptionError: clave equivocada, texto alterado o formato corrupto. Se
                envuelve la excepción de `joserfc` porque su tipo es un detalle de
                implementación de la librería, y el llamador tiene que poder distinguir este
                caso de "el valor no coincide".
        """
        from joserfc import jwe

        try:
            claro = jwe.decrypt_compact(ciphertext, self._clave).plaintext
            if claro is None:  # pragma: no cover - no debería pasar con `dir` + A256GCM
                raise ValueError("el JWE no trajo texto en claro")
            return claro.decode("utf-8")
        except Exception as exc:
            raise SecretDecryptionError(
                "No se pudo descifrar el valor. O cambió `secret_key` sin migrar las filas, o "
                "la columna se alteró."
            ) from exc

    def encrypt_optional(self, plaintext: str | None) -> str | None:
        """`None` pasa derecho. Existe para no ramificar en cada llamador."""
        return None if plaintext is None else self.encrypt(plaintext)

    def decrypt_optional(self, ciphertext: str | None) -> str | None:
        """`None` pasa derecho."""
        return None if ciphertext is None else self.decrypt(ciphertext)
