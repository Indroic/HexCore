"""
Los flujos de `passkey`: registrar, autenticar, listar y borrar.

WebAuthn es un protocolo de dos pasos, y los dos pasos están en cada flujo: primero se emiten
opciones con un desafío, después se verifica la respuesta del autenticador contra ese desafío. El
desafío es lo que ata la firma a **este** intento, así que se guarda del lado del servidor, en
claro y de un solo uso. En claro porque es un nonce público —ver el mixin— y de un solo uso porque
canjearlo dos veces sería poder replayear la aserción.

**La propiedad que hace a las passkeys distintas de todo lo demás en Darwin: la credencial que se
guarda es pública.** No hay nada en `darwin_passkey` que un atacante con un dump pueda usar para
autenticarse — ni acá ni en otro sitio. Es lo contrario del secreto TOTP (compartido, va cifrado) y
del hash de contraseña (no reversible pero atacable por diccionario). Es la razón por la que las
passkeys son la mejor opción disponible, y el motivo de que este plugin no tenga ningún secreto que
proteger.

⚠️ **El contador de firmas es la única señal de compromiso que el protocolo da.** Un contador que
no avanza significa autenticador clonado o aserción replayeada, y la respuesta correcta no es
"reintentá": es cortar. Muchas implementaciones lo descartan porque "algunos autenticadores no lo
incrementan"; acá se distingue el autenticador que nunca lo usa (contador 0 siempre, se acepta) del
que lo usaba y dejó de avanzar (se rechaza).
"""
from __future__ import annotations

import typing as t
from datetime import timedelta
from uuid import UUID, uuid4

from hexcore.darwin.plugins.passkey.domain import (
    Passkey,
    PasskeyAlreadyRegisteredError,
    PasskeyChallenge,
    PasskeyChallengeError,
    PasskeyClonedAuthenticatorError,
    PasskeyLastFactorError,
    PasskeyNotFoundError,
    PasskeyVerificationError,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.context import Transport
    from hexcore.darwin.domain.entities import IdentitySession, User
    from hexcore.darwin.domain.ports import (
        AbstractAccountRepository,
        AbstractClock,
        AbstractUserRepository,
    )
    from hexcore.darwin.domain.value_objects import TokenPair
    from hexcore.darwin.plugins.passkey.domain import (
        AbstractPasskeyChallengeRepository,
        AbstractPasskeyRepository,
        AbstractWebAuthnVerifier,
    )

__all__ = ["DEFAULT_CHALLENGE_TTL", "PasskeyOptions", "PasskeySignIn", "PasskeyService"]

#: Cuánto vive un desafío.
#:
#: 5 minutos: el diálogo del navegador tiene su propio timeout de 60 segundos, pero el usuario
#: puede tener que buscar la llave, conectarla, o resolver la biometría en otro dispositivo. Más
#: largo sería una ventana en la que un desafío filtrado sigue sirviendo.
DEFAULT_CHALLENGE_TTL = timedelta(minutes=5)


class PasskeyOptions(t.NamedTuple):
    """
    Lo que se le pasa a `navigator.credentials.create()` o `.get()`.

    El desafío **no** está acá como campo aparte: va dentro de `options` —el navegador lo
    necesita— y la fila quedó en la base. El cliente no tiene que mandarlo de vuelta por su
    cuenta: viene firmado adentro del `clientDataJSON`, que es lo único que el servidor mira.
    """

    options: dict[str, t.Any]


class PasskeySignIn(t.NamedTuple):
    """El resultado de autenticar con una passkey."""

    user: "User"
    passkey: Passkey
    session: "IdentitySession"
    tokens: "TokenPair"


class PasskeyService:
    """
    Los flujos de passkeys.

    Uso::

        servicio = get_passkey_service()
        opciones = await servicio.start_registration(user_id=usuario.id, user_name=usuario.email)
        # ...el navegador responde
        await servicio.finish_registration(credential=respuesta, name="iPhone de Ana")
    """

    def __init__(
        self,
        *,
        passkeys: "AbstractPasskeyRepository",
        challenges: "AbstractPasskeyChallengeRepository",
        users: "AbstractUserRepository",
        accounts: "AbstractAccountRepository",
        sessions: t.Any,
        clock: "AbstractClock",
        verifier: "AbstractWebAuthnVerifier",
        challenge_ttl: timedelta = DEFAULT_CHALLENGE_TTL,
    ) -> None:
        self._passkeys = passkeys
        self._challenges = challenges
        self._users = users
        self._accounts = accounts
        self._sessions = sessions
        self._clock = clock
        self._verifier = verifier
        self._ttl = challenge_ttl

    # ── Registro ──────────────────────────────────────────────────────────────
    async def start_registration(
        self, *, user_id: UUID, user_name: str
    ) -> PasskeyOptions:
        """
        Emite las opciones de registro y guarda el desafío.

        `excludeCredentials` va con lo que el usuario ya tiene: sin eso, el navegador le ofrece
        registrar de nuevo una credencial existente, y el flujo falla al guardar con un error de
        base en vez de un mensaje.
        """
        existentes = await self._passkeys.list_for_user(user_id)
        opciones, desafio = self._verifier.registration_options(
            user_id=user_id,
            user_name=user_name,
            exclude_credential_ids=[p.credential_id for p in existentes],
        )
        await self._guardar_desafio(desafio, purpose="register", user_id=user_id)
        return PasskeyOptions(options=opciones)

    async def finish_registration(
        self, *, credential: t.Mapping[str, t.Any], name: str | None = None
    ) -> Passkey:
        """
        Verifica la respuesta del autenticador y guarda la credencial.

        El `user_id` sale del **desafío**, no del cuerpo del request: aceptarlo del cliente dejaría
        registrar una credencial propia en la cuenta de otro, que es toma de cuenta directa.

        Raises:
            PasskeyChallengeError: el desafío no existe, venció o ya se usó.
            PasskeyVerificationError: la respuesta no verifica.
            PasskeyAlreadyRegisteredError: esa credencial ya está registrada, acá o en otra cuenta.
        """
        desafio = await self._consumir_desafio(credential, purpose="register")
        if desafio.user_id is None:  # pragma: no cover - `start_registration` siempre lo pone
            raise PasskeyChallengeError("El desafío de registro no tiene usuario.")

        registrada = self._verifier.verify_registration(
            credential=credential,
            expected_challenge=_desafio_en_bytes(desafio),
        )

        ya_existe = await self._passkeys.get_by_credential_id(registrada.credential_id)
        if ya_existe is not None:
            # No se mueve, ni siquiera si es del mismo usuario: si es de otro, moverla le saca un
            # método de acceso; si es del mismo, `excludeCredentials` debería haberlo evitado y
            # sobreescribir el contador reiniciaría la detección de clonado.
            raise PasskeyAlreadyRegisteredError(
                "Esa credencial ya está registrada."
            )

        return await self._passkeys.add(
            Passkey(
                id=uuid4(),
                user_id=desafio.user_id,
                credential_id=registrada.credential_id,
                public_key=registrada.public_key,
                sign_count=registrada.sign_count,
                name=(name or "").strip() or None,
                aaguid=registrada.aaguid,
                backed_up=registrada.backed_up,
                transports=registrada.transports,
            )
        )

    # ── Autenticación ─────────────────────────────────────────────────────────
    async def start_authentication(
        self, *, user_id: UUID | None = None
    ) -> PasskeyOptions:
        """
        Emite las opciones de login y guarda el desafío.

        Con `user_id`, el navegador se limita a las credenciales de ese usuario. **Sin él** es el
        flujo con credenciales descubribles —el "usernameless"— donde el navegador ofrece lo que
        tenga y el servidor descubre quién es al verificar. Es el que da la mejor experiencia, y el
        que obliga a que el desafío se pueda canjear sin saber de quién es.
        """
        permitidas: list[str] = []
        if user_id is not None:
            permitidas = [p.credential_id for p in await self._passkeys.list_for_user(user_id)]

        opciones, desafio = self._verifier.authentication_options(
            allow_credential_ids=permitidas
        )
        await self._guardar_desafio(desafio, purpose="authenticate", user_id=user_id)
        return PasskeyOptions(options=opciones)

    async def finish_authentication(
        self,
        *,
        credential: t.Mapping[str, t.Any],
        transport: "Transport" = "cookie",
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> PasskeySignIn:
        """
        Verifica la aserción y abre la sesión.

        El orden importa: se consume el desafío, se busca la credencial, se verifica la firma, y
        **recién después** se sube el contador. Subirlo antes de verificar dejaría que una firma
        inválida avance el contador y desincronice al autenticador legítimo.

        Raises:
            PasskeyChallengeError: el desafío no sirve.
            PasskeyNotFoundError: la credencial no está registrada.
            PasskeyVerificationError: la firma no valida.
            PasskeyClonedAuthenticatorError: el contador no avanzó. Ver el docstring del módulo.
        """
        desafio = await self._consumir_desafio(credential, purpose="authenticate")

        crudo = credential.get("id") or credential.get("rawId")
        if not isinstance(crudo, str) or not crudo:
            raise PasskeyVerificationError("La credencial no trae identificador.")

        guardada = await self._passkeys.get_by_credential_id(crudo)
        if guardada is None:
            raise PasskeyNotFoundError("Esa credencial no está registrada.")

        # Si el desafío se emitió para un usuario concreto, la credencial tiene que ser suya. Sin
        # este chequeo, alguien pide un desafío "para Ana" y lo completa con su propia
        # credencial: la firma valida y el desafío también, y entraría como él mismo — o peor,
        # dependiendo de qué usuario tome el llamador.
        if desafio.user_id is not None and guardada.user_id != desafio.user_id:
            raise PasskeyVerificationError(
                "Esa credencial no corresponde al usuario del desafío."
            )

        verificada = self._verifier.verify_authentication(
            credential=credential,
            expected_challenge=_desafio_en_bytes(desafio),
            public_key=guardada.public_key,
            current_sign_count=guardada.sign_count,
        )

        ahora = self._clock.now()
        if not await self._passkeys.bump_sign_count(
            guardada.credential_id, new_count=verificada.new_sign_count, at=ahora
        ):
            raise PasskeyClonedAuthenticatorError(
                "El contador de firmas de la credencial no avanzó. Puede estar clonada o la "
                "respuesta puede ser un reenvío. Por seguridad no se abre la sesión: revisá tus "
                "credenciales registradas."
            )

        usuario = await self._users.get_by_id(guardada.user_id)
        if usuario is None:
            raise PasskeyNotFoundError("El dueño de la credencial ya no existe.")

        sesion, par = await self._sessions.create(
            actor=usuario,
            transport=transport,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return PasskeySignIn(
            user=usuario, passkey=guardada, session=sesion, tokens=par
        )

    # ── Ciclo de vida ─────────────────────────────────────────────────────────
    async def list_for_user(self, user_id: UUID) -> list[Passkey]:
        """Las credenciales del usuario, para la pantalla de ajustes."""
        return await self._passkeys.list_for_user(user_id)

    async def delete(self, *, user_id: UUID, passkey_id: UUID) -> None:
        """
        Borra una credencial del usuario.

        ⚠️ **Se niega a dejar la cuenta sin ningún método de acceso.** Borrar la última passkey de
        alguien que no tiene contraseña ni proveedor vinculado lo deja afuera de su propia cuenta,
        y el botón está a un click en cualquier pantalla de ajustes.

        Raises:
            PasskeyNotFoundError: no es suya, o no existe. **El mismo error para los dos**: un 403
                distinto le confirmaría a quien prueba ids que la credencial existe.
            PasskeyLastFactorError: es el último método de acceso.
        """
        propias = await self._passkeys.list_for_user(user_id)
        objetivo = next((p for p in propias if p.id == passkey_id), None)
        if objetivo is None:
            raise PasskeyNotFoundError("No se encontró esa credencial.")

        if len(propias) == 1 and not await self._tiene_otro_metodo(user_id):
            raise PasskeyLastFactorError(
                "No se puede borrar la única credencial de la cuenta: quedaría sin forma de "
                "iniciar sesión. Poné una contraseña, vinculá un proveedor, o registrá otra "
                "passkey primero."
            )

        await self._passkeys.delete(passkey_id)

    async def _tiene_otro_metodo(self, user_id: UUID) -> bool:
        """
        Si el usuario tiene otra forma de entrar: contraseña o un proveedor vinculado.

        Se consulta `account` y no una tabla propia porque es donde viven los dos: la credencial
        local con su hash y cada identidad de OAuth. Es el mismo chequeo que hace `oauth.unlink`, y
        que estén los dos es deliberado — el usuario puede borrar su última passkey desde la
        pantalla de passkeys y su último proveedor desde la de proveedores.
        """
        cuentas = await self._accounts.list_for_user(user_id)
        return any(
            (c.is_credential and c.password) or not c.is_credential for c in cuentas
        )

    # ── Interno ───────────────────────────────────────────────────────────────
    async def _guardar_desafio(
        self, desafio: bytes, *, purpose: t.Any, user_id: UUID | None
    ) -> None:
        from hexcore.darwin.plugins.passkey.webauthn_adapter import b64url_encode

        ahora = self._clock.now()
        await self._challenges.add(
            PasskeyChallenge(
                id=uuid4(),
                challenge=b64url_encode(desafio),
                purpose=purpose,
                user_id=user_id,
                expires_at=ahora + self._ttl,
            )
        )

    async def _consumir_desafio(
        self, credential: t.Mapping[str, t.Any], *, purpose: t.Any
    ) -> PasskeyChallenge:
        """
        Canjea el desafío que viene dentro del `clientDataJSON` de la respuesta.

        El desafío no viaja como campo aparte: está **adentro** del `clientDataJSON`, que es
        justamente lo que el autenticador firma. Leerlo de ahí es lo que permite encontrar la fila
        sin que el cliente mande un identificador aparte que podría no corresponder.

        Lo que se devuelve es **la fila**, y el `expected_challenge` del verificador sale de ella
        y no de acá: así la comparación del verificador es contra el valor que el servidor emitió,
        y no contra el que mandó el cliente consigo mismo.
        """
        crudo = _leer_desafio_del_cliente(credential)
        if crudo is None:
            raise PasskeyChallengeError(
                "La respuesta del autenticador no trae un desafío legible."
            )

        consumido = await self._challenges.consume(
            purpose, crudo, at=self._clock.now()
        )
        if consumido is None:
            raise PasskeyChallengeError(
                "El desafío no es válido, venció o ya se usó. Volvé a empezar."
            )
        return consumido


def _leer_desafio_del_cliente(
    credential: t.Mapping[str, t.Any],
) -> str | None:
    """
    El `challenge` del `clientDataJSON`, en base64url.

    **No se verifica nada acá.** Este valor sirve sólo para *encontrar* la fila del desafío; la
    verificación criptográfica —que el `clientDataJSON` esté firmado y que su `challenge` sea el
    esperado— la hace el verificador después, con el valor que salió de la base. Leerlo así es
    tratar la entrada del cliente como una clave de búsqueda y nada más.
    """
    import base64
    import json

    crudo_respuesta = credential.get("response")
    if not isinstance(crudo_respuesta, dict):
        return None

    respuesta = t.cast("dict[str, t.Any]", crudo_respuesta)
    client_data = respuesta.get("clientDataJSON")
    if not isinstance(client_data, str) or not client_data:
        return None

    try:
        bruto = base64.urlsafe_b64decode(
            client_data + "=" * (-len(client_data) % 4)
        )
        datos = json.loads(bruto)
    except Exception:
        # Entrada del cliente: un JSON corrupto es un 401, no un 500.
        return None

    if not isinstance(datos, dict):
        return None
    valor = t.cast("dict[str, t.Any]", datos).get("challenge")
    return valor if isinstance(valor, str) and valor else None


def _desafio_en_bytes(challenge: PasskeyChallenge) -> bytes:
    """
    El desafío que el verificador tiene que esperar, **sacado de la fila**.

    De la fila y no del `clientDataJSON`: así el verificador compara el `clientDataJSON` firmado
    contra el valor que el servidor emitió, que es la comparación que el protocolo pide. Tomarlo
    del cliente lo compararía consigo mismo.
    """
    from hexcore.darwin.plugins.passkey.webauthn_adapter import b64url_decode

    return b64url_decode(challenge.challenge)
