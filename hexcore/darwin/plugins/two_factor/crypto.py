"""
Cifrado del secreto TOTP en reposo.

**El secreto TOTP no se puede hashear**, y eso obliga la decisión entera: para verificar un
código hay que recalcularlo, así que el secreto tiene que poder recuperarse. Un `token_hash`
—lo que hace el resto de Darwin— no aplica acá.

Guardado en claro, un dump de la base es el segundo factor de todos los usuarios: quien lo tenga
genera códigos válidos indefinidamente, y rotarlo obliga a que cada usuario vuelva a escanear un
QR. Cifrado con una clave que vive en la aplicación, ese mismo dump no sirve para nada — que es
la única propiedad que se puede tener acá.

El cifrado en sí lo hace `SecretBox` (JWE `dir` + `A256GCM`, AEAD, clave derivada por etiqueta).
Este módulo es la etiqueta y nada más: existe para que el propósito quede nombrado y para que
`_ETIQUETA` no se pueda tocar sin ver el comentario de que cambiarla deja a todos los usuarios
sin segundo factor.
"""
from __future__ import annotations

from hexcore.darwin.infrastructure.secretbox import SecretBox, SecretDecryptionError

__all__ = ["TotpSecretCipher", "SecretDecryptionError"]

#: La etiqueta de derivación. ⚠️ **Cambiarla invalida todos los secretos TOTP guardados** y deja a
#: cada usuario con 2FA activo afuera de su cuenta. Va versionada por eso.
_ETIQUETA = b"hexcore.darwin.two_factor.secret.v1"


class TotpSecretCipher(SecretBox):
    """
    Cifra y descifra secretos TOTP.

    Uso::

        cifrador = TotpSecretCipher("una-clave-de-firma-larga")
        guardado = cifrador.encrypt("JBSWY3DPEHPK3PXP")
        assert cifrador.decrypt(guardado) == "JBSWY3DPEHPK3PXP"
    """

    __slots__ = ()

    def __init__(self, secret_key: str) -> None:
        super().__init__(secret_key, label=_ETIQUETA)
