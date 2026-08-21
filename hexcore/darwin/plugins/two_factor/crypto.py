"""
Cifrado del secreto TOTP en reposo.

**El secreto TOTP no se puede hashear**, y eso obliga la decisión entera: para verificar un
código hay que recalcularlo, así que el secreto tiene que poder recuperarse. Un `token_hash`
—lo que hace el resto de Darwin— no aplica acá.

Guardado en claro, un dump de la base es el segundo factor de todos los usuarios: quien lo tenga
genera códigos válidos indefinidamente, y rotarlo obliga a que cada usuario vuelva a escanear un
QR. Cifrado con una clave que vive en la aplicación, ese mismo dump no sirve para nada — que es
la única propiedad que se puede tener acá.

Se usa **JWE compacto `dir` + `A256GCM` de `joserfc`**, que ya es una dependencia del extra
`[darwin]` porque firma los tokens. Es AEAD, así que el texto cifrado está autenticado: alguien
con acceso de escritura a la base no puede sustituir el secreto de un usuario por uno que él
conoce sin que el descifrado falle. Un XOR con una clave derivada, o cualquier cifrado sin MAC,
dejaría esa puerta abierta. Y no se agrega `cryptography` como dependencia nueva para algo que
la que ya está resuelve.

La clave de cifrado es **derivada y no `secret_key` directo**: HKDF con una etiqueta propia, así
que la clave que cifra secretos TOTP no es la misma que firma sobres ni la que deriva valores
anti-CSRF. Reusar el mismo material para tres propósitos hace que romper uno rompa los tres.
"""
from __future__ import annotations

import hashlib
import hmac
import typing as t

__all__ = ["TotpSecretCipher", "SecretDecryptionError"]

#: La etiqueta de derivación. Cambiarla invalida todo lo cifrado antes, así que es una
#: constante y no un parámetro.
_ETIQUETA = b"hexcore.darwin.two_factor.secret.v1"

#: La cabecera del JWE. `dir` significa "la clave de contenido es la que doy", que es lo
#: correcto acá: no hay un segundo destinatario al que envolverle una clave.
_CABECERA: dict[str, t.Any] = {"alg": "dir", "enc": "A256GCM"}


class SecretDecryptionError(Exception):
    """
    No se pudo descifrar el secreto TOTP.

    Pasa en dos casos, y los dos son operativos y no del usuario: cambió `secret_key` sin
    migrar las filas, o alguien alteró la columna. Se distingue de un código inválido a
    propósito: decirle "código incorrecto" a un usuario cuya fila no se puede descifrar lo deja
    reintentando para siempre contra un problema que él no puede resolver.
    """


class TotpSecretCipher:
    """
    Cifra y descifra secretos TOTP con una clave derivada de `secret_key`.

    Uso::

        cifrador = TotpSecretCipher("una-clave-de-firma-larga")
        guardado = cifrador.encrypt("JBSWY3DPEHPK3PXP")
        assert cifrador.decrypt(guardado) == "JBSWY3DPEHPK3PXP"
    """

    __slots__ = ("_clave",)

    def __init__(self, secret_key: str) -> None:
        from joserfc.jwk import OctKey

        # HKDF-Extract/Expand reducido a un solo HMAC: la entrada ya tiene 256 bits de
        # entropía —`secret_key` se valida con un mínimo de largo— así que la expansión
        # iterada de HKDF no agrega nada. Lo que sí importa es que la etiqueta separe este
        # propósito de los demás.
        material = hmac.new(
            secret_key.encode("utf-8"), _ETIQUETA, hashlib.sha256
        ).digest()
        self._clave = OctKey.import_key(material)

    def encrypt(self, secret: str) -> str:
        """El secreto como JWE compacto. Cada llamada da un texto distinto (nonce nuevo)."""
        from joserfc import jwe

        return jwe.encrypt_compact(_CABECERA, secret.encode("ascii"), self._clave)

    def decrypt(self, ciphertext: str) -> str:
        """
        El secreto en claro.

        Raises:
            SecretDecryptionError: clave equivocada, texto alterado o formato corrupto. Se
                envuelve la excepción de `joserfc` porque su tipo es un detalle de
                implementación de la librería y el llamador tiene que poder distinguir este
                caso de un código incorrecto.
        """
        from joserfc import jwe

        try:
            claro = jwe.decrypt_compact(ciphertext, self._clave).plaintext
            if claro is None:  # pragma: no cover - no debería pasar con `dir` + A256GCM
                raise ValueError("el JWE no trajo texto en claro")
            return claro.decode("ascii")
        except Exception as exc:
            raise SecretDecryptionError(
                "No se pudo descifrar el secreto TOTP. O cambió `secret_key` sin migrar las "
                "filas de `darwin_two_factor`, o la columna se alteró. Los usuarios afectados "
                "tienen que volver a inscribir su segundo factor."
            ) from exc
