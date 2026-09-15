"""
`AbstractWebAuthnVerifier` sobre `py_webauthn`. Requiere el extra `[darwin-passkey]`.

Cuatro decisiones, y las cuatro son sobre qué **no** se deja pasar:

1. **`expected_origin` es obligatorio y es una lista explícita.** Es el chequeo que hace a WebAuthn
   resistente al phishing: el navegador firma el origen real, y si no se compara, un sitio clonado
   puede reenviar la aserción. `py_webauthn` lo pide, y acá se exige al construir el adaptador —no
   al primer login— porque un despliegue sin orígenes declarados no tiene ninguna protección.
2. **`attestation="none"` por default.** Pedir attestation obliga a mantener cadenas de
   certificados de fabricante y rechaza autenticadores de plataforma perfectamente válidos. Sirve
   para exigir hardware certificado en un entorno regulado, y para nada más — y ese caso se pide
   explícito.
3. **`user_verification="preferred"` por default, no `required`.** `required` rechaza a quien tiene
   una llave sin PIN ni biometría, que es válida como *segundo* factor. Para el login sin
   contraseña sí conviene `required`, y el plugin lo expone.
4. **El detalle del error va al log, no a la respuesta.** `py_webauthn` levanta excepciones que
   dicen exactamente qué chequeo falló, y devolverlas al cliente es darle el camino para el
   siguiente intento.
"""
from __future__ import annotations

import base64
import logging
import secrets
import typing as t
from uuid import UUID

from hexcore.darwin.plugins.passkey.domain import (
    AbstractWebAuthnVerifier,
    PasskeyVerificationError,
    RegisteredCredential,
    VerifiedAssertion,
)

__all__ = ["PyWebAuthnVerifier", "b64url_encode", "b64url_decode", "CHALLENGE_BYTES"]

logger = logging.getLogger("hexcore.darwin.passkey")

#: 32 bytes. La spec pide al menos 16; 32 no cuesta nada y saca el tema de discusión.
CHALLENGE_BYTES = 32


def b64url_encode(datos: bytes) -> str:
    """base64url **sin relleno**, que es lo que usa WebAuthn en todos sus campos."""
    return base64.urlsafe_b64encode(datos).decode("ascii").rstrip("=")


def b64url_decode(valor: str) -> bytes:
    """
    De base64url a bytes, tolerando el relleno faltante.

    Tolerante porque el valor puede venir de un cliente que sí lo incluye: la spec dice sin
    relleno, y hay librerías de navegador que lo agregan igual.
    """
    return base64.urlsafe_b64decode(valor + "=" * (-len(valor) % 4))


class PyWebAuthnVerifier(AbstractWebAuthnVerifier):
    """
    El verificador real.

    Args:
        rp_id: El dominio de la Relying Party (`"mi-app.com"`). **Sin esquema y sin puerto**, y no
            puede ser un sufijo de otro dominio: el navegador exige que el origen sea `rp_id` o un
            subdominio, así que poner `"com"` no funciona (y por suerte).
        rp_name: El nombre que muestra el navegador en el diálogo.
        origins: Los orígenes completos permitidos (`"https://mi-app.com"`). **Obligatorio.**
        require_user_verification: Si se exige PIN o biometría. Ponelo en `True` para login sin
            contraseña; dejalo en `False` si las passkeys son un segundo factor.
        timeout_ms: Cuánto espera el navegador antes de cancelar el diálogo.

    Uso::

        from hexcore.darwin.plugins.passkey.webauthn_adapter import PyWebAuthnVerifier

        verificador = PyWebAuthnVerifier(
            rp_id="mi-app.com",
            rp_name="Mi App",
            origins=["https://mi-app.com"],
        )
    """

    def __init__(
        self,
        *,
        rp_id: str,
        rp_name: str = "HexCore",
        origins: t.Sequence[str],
        require_user_verification: bool = False,
        timeout_ms: int = 60_000,
    ) -> None:
        if not rp_id.strip():
            raise ValueError("`rp_id` no puede estar vacío.")
        if not origins:
            raise ValueError(
                "`origins` es obligatorio: es el chequeo que hace a WebAuthn resistente al "
                "phishing. Sin él, un sitio clonado puede reenviar la aserción del usuario.\n\n"
                '    PyWebAuthnVerifier(rp_id="mi-app.com", origins=["https://mi-app.com"])'
            )

        self._rp_id = rp_id
        self._rp_name = rp_name
        self._origins = list(origins)
        self._uv = require_user_verification
        self._timeout = timeout_ms

    # ── Registro ──────────────────────────────────────────────────────────────
    def registration_options(
        self,
        *,
        user_id: UUID,
        user_name: str,
        exclude_credential_ids: t.Sequence[str] = (),
    ) -> tuple[dict[str, t.Any], bytes]:
        import json

        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )

        desafio = secrets.token_bytes(CHALLENGE_BYTES)
        opciones = generate_registration_options(
            rp_id=self._rp_id,
            rp_name=self._rp_name,
            user_id=user_id.bytes,
            user_name=user_name,
            challenge=desafio,
            timeout=self._timeout,
            authenticator_selection=AuthenticatorSelectionCriteria(
                # `preferred` y no `required`: `required` rechaza llaves de seguridad que no
                # guardan la credencial, que sirven perfectamente como segundo factor. Con
                # `preferred`, el autenticador que puede la hace descubrible —y habilita el login
                # sin usuario declarado— y el que no, igual registra.
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=(
                    UserVerificationRequirement.REQUIRED
                    if self._uv
                    else UserVerificationRequirement.PREFERRED
                ),
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=b64url_decode(cid))
                for cid in exclude_credential_ids
            ],
        )
        return json.loads(options_to_json(opciones)), desafio

    def verify_registration(
        self, *, credential: t.Mapping[str, t.Any], expected_challenge: bytes
    ) -> RegisteredCredential:
        from webauthn import verify_registration_response

        try:
            verificado = verify_registration_response(
                credential=dict(credential),
                expected_challenge=expected_challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origins,
                require_user_verification=self._uv,
            )
        except Exception as exc:
            # El detalle va al log. `py_webauthn` dice exactamente qué chequeo falló, y devolverlo
            # al cliente es darle el camino para el siguiente intento.
            logger.warning("Falló la verificación del registro: %s", exc)
            raise PasskeyVerificationError(
                "No se pudo verificar la credencial. Probá de nuevo."
            ) from exc

        # `transports` sale del cliente y no de la verificación: `py_webauthn` no lo devuelve
        # porque no está firmado — es una pista del navegador sobre cómo alcanzar el
        # autenticador. Se guarda igual, porque el navegador la usa para decidir si ofrece NFC o
        # USB en el próximo login, pero **no se trata como dato de confianza**.
        crudo_respuesta = credential.get("response")
        transportes: tuple[str, ...] = ()
        if isinstance(crudo_respuesta, dict):
            crudo = t.cast("dict[str, t.Any]", crudo_respuesta).get("transports")
            if isinstance(crudo, (list, tuple)):
                transportes = tuple(
                    str(x) for x in t.cast("t.Iterable[t.Any]", crudo)
                )

        return RegisteredCredential(
            credential_id=b64url_encode(verificado.credential_id),
            public_key=b64url_encode(verificado.credential_public_key),
            sign_count=verificado.sign_count,
            aaguid=str(verificado.aaguid) if verificado.aaguid else None,
            backed_up=bool(verificado.credential_backed_up),
            transports=transportes,
            user_verified=bool(verificado.user_verified),
        )

    # ── Autenticación ─────────────────────────────────────────────────────────
    def authentication_options(
        self, *, allow_credential_ids: t.Sequence[str] = ()
    ) -> tuple[dict[str, t.Any], bytes]:
        import json

        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import (
            PublicKeyCredentialDescriptor,
            UserVerificationRequirement,
        )

        desafio = secrets.token_bytes(CHALLENGE_BYTES)
        opciones = generate_authentication_options(
            rp_id=self._rp_id,
            challenge=desafio,
            timeout=self._timeout,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=b64url_decode(cid))
                for cid in allow_credential_ids
            ]
            or None,
            user_verification=(
                UserVerificationRequirement.REQUIRED
                if self._uv
                else UserVerificationRequirement.PREFERRED
            ),
        )
        return json.loads(options_to_json(opciones)), desafio

    def verify_authentication(
        self,
        *,
        credential: t.Mapping[str, t.Any],
        expected_challenge: bytes,
        public_key: str,
        current_sign_count: int,
    ) -> VerifiedAssertion:
        from webauthn import verify_authentication_response

        try:
            verificado = verify_authentication_response(
                credential=dict(credential),
                expected_challenge=expected_challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origins,
                credential_public_key=b64url_decode(public_key),
                credential_current_sign_count=current_sign_count,
                require_user_verification=self._uv,
            )
        except Exception as exc:
            logger.warning("Falló la verificación de la aserción: %s", exc)
            raise PasskeyVerificationError(
                "No se pudo verificar la credencial. Probá de nuevo."
            ) from exc

        return VerifiedAssertion(
            credential_id=b64url_encode(verificado.credential_id),
            new_sign_count=verificado.new_sign_count,
            user_verified=bool(verificado.user_verified),
        )
