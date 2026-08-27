"""
Emisión y verificación de JWT.

**La regla de la que sale todo lo demás: el algoritmo lo decide el verificador, nunca el
token.** `joserfc` obliga a pasar la lista de algoritmos permitidos —por eso se eligió sobre
`pyjwt`, cuya API la acepta como opcional en algunas rutas— así que el default seguro es
estructural y no documental.

Ataques que la verificación rechaza explícitamente, cada uno con test:

- ``alg: none``: un token sin firma. Nunca está en la allowlist.
- **Confusión HS/RS**: firmar HS256 usando la clave *pública* Ed25519 como secreto HMAC. Se
  evita porque la allowlist no incluye HS* cuando el almacén es asimétrico, y porque los
  almacenes están separados.
- **Confusión de `typ`**: presentar un refresh token donde se espera un access token. El
  refresh vive mucho más, así que el `exp` corto del access dejaría de servir para nada.
- **Confusión de transporte**: replayear una cookie como `Authorization: Bearer`, esquivando
  `SameSite` y el chequeo CSRF. Lo corta el `aud`.
- **`kid` desconocido en volumen**: un flood de `kid` inventados sería un ataque de
  amplificación contra el almacén de claves. Lo corta la caché negativa.
- **`jku` / `x5u` / `jwk` embebidos**: nunca se leen. Honrarlos deja que el token elija con qué
  clave se verifica, que es la vulnerabilidad completa.
"""
from __future__ import annotations

import typing as t
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from hexcore.darwin.domain.exceptions import (
    TokenAudienceMismatchError,
    TokenExpiredError,
    TokenMalformedError,
)
from hexcore.darwin.domain.value_objects import AccessTokenClaims, TokenType

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.context import AuthContext, Transport
    from hexcore.darwin.domain.ports import AbstractClock
    from hexcore.darwin.infrastructure.keys import AbstractKeyStore

__all__ = [
    "TokenTtl",
    "JoserfcTokenIssuer",
    "JoserfcTokenVerifier",
    "audience_for",
]

#: Allowlist por defecto. **Sólo asimétricos.** Un HS* acá habilitaría la confusión de
#: algoritmo: el atacante toma la clave pública —que es pública— y la usa como secreto HMAC.
#:
#: `Ed25519` primero (RFC 9864). `EdDSA` **no** está: es el identificador que ese RFC deprecó,
#: y aceptarlo en la allowlist de un módulo nuevo sería nacer con deuda. Un despliegue que ya
#: tenga tokens con `alg: EdDSA` lo agrega explícito durante la migración.
DEFAULT_ALLOWED_ALGORITHMS: tuple[str, ...] = ("Ed25519", "Ed448", "RS256", "PS256", "ES256")

#: Tolerancia de desfase de reloj entre el emisor y el verificador. Se aplica **sólo** a la
#: ventana temporal (`exp`/`nbf`), nunca a la revocación: un margen ahí sería una ventana de
#: uso para un token ya revocado.
DEFAULT_LEEWAY = timedelta(seconds=30)


class TokenTtl(t.NamedTuple):
    """
    Vidas de los tokens.

    El access token vive **120 segundos** por defecto, y ese número es el que hace viable la
    revocación en tres capas: con un `exp` así de corto, el camino caliente no necesita
    consultar la base, porque el peor caso de un token revocado que sigue sirviendo son dos
    minutos. Subirlo a una hora convierte la revocación en algo que hay que consultar por
    request.
    """

    access: timedelta = timedelta(seconds=120)
    refresh: timedelta = timedelta(days=30)


def audience_for(issuer: str, transport: "Transport") -> str:
    """
    El `aud` de un token, derivado del transporte.

    Es lo que ata el token a su vía de entrega. Sin esto, una cookie robada se puede
    presentar como `Authorization: Bearer` y esquiva `SameSite` y el chequeo anti-CSRF de una
    sola vez, porque el camino Bearer no los aplica.
    """
    return f"{issuer}#{transport}"


class JoserfcTokenIssuer:
    """
    Emite tokens firmados con la clave `active` del almacén.

    Uso::

        emisor = JoserfcTokenIssuer(issuer="https://api.ejemplo.com", key_store=almacen)
        token = await emisor.issue_access(contexto, session_id=sid, generation=0)
    """

    def __init__(
        self,
        *,
        issuer: str,
        key_store: "AbstractKeyStore",
        clock: "AbstractClock | None" = None,
        ttl: TokenTtl | None = None,
    ) -> None:
        self._issuer = issuer
        self._keys = key_store
        self._ttl = ttl or TokenTtl()
        if clock is None:
            from hexcore.darwin.infrastructure.clock import SystemClock

            clock = SystemClock()
        self._clock = clock

    async def issue_access(
        self,
        context: "AuthContext[t.Any]",
        *,
        session_id: UUID,
        generation: int = 0,
        scopes: t.Iterable[str] | None = None,
    ) -> str:
        """
        Emite un access token para `context`.

        `act` y `sub` salen del contexto por separado, así que la impersonación **sobrevive al
        token**. Derivarlos los dos del sujeto perdería al actor, y a partir de ahí la acción
        queda atribuida a la víctima.
        """
        return await self._emitir(
            context, session_id=session_id, generation=generation, scopes=scopes,
            typ="at+jwt", ttl=self._ttl.access,
        )

    async def issue_refresh(
        self,
        context: "AuthContext[t.Any]",
        *,
        session_id: UUID,
        generation: int = 0,
    ) -> str:
        """
        Emite un refresh token.

        Sin `scopes` a propósito: un refresh no autoriza nada, sólo canjea. Meterle permisos
        haría que un refresh robado sirviera para actuar, no sólo para renovar.
        """
        return await self._emitir(
            context, session_id=session_id, generation=generation, scopes=(),
            typ="rt+jwt", ttl=self._ttl.refresh,
        )

    async def _emitir(
        self,
        context: "AuthContext[t.Any]",
        *,
        session_id: UUID,
        generation: int,
        scopes: t.Iterable[str] | None,
        typ: TokenType,
        ttl: timedelta,
    ) -> str:
        from joserfc import jwt
        from joserfc.jwk import KeySet

        from hexcore.darwin.domain.context import Principal

        if not isinstance(context.actor, Principal) or not isinstance(
            context.subject, Principal
        ):
            raise TokenMalformedError(
                "No se puede emitir un token para un principal de sistema: no tiene sesión "
                "ni identidad de usuario. Los procesos automáticos usan `system_context()`, "
                "que no necesita token."
            )

        clave = await self._keys.get_active()
        ahora = self._clock.now()
        emitido = int(ahora.timestamp())

        claims = AccessTokenClaims(
            iss=self._issuer,
            sub=context.subject.user_id,
            act=context.actor.user_id,
            sid=session_id,
            aud=audience_for(self._issuer, context.transport),
            typ=typ,
            gen=generation,
            iat=emitido,
            nbf=emitido,
            exp=int((ahora + ttl).timestamp()),
            jti=uuid4(),
            scopes=frozenset(scopes or ()),
            imp=context.is_impersonating,
        )

        conjunto = KeySet.import_key_set({"keys": [_jwk(clave.private_key)]})
        # `algorithms` explícito: `joserfc` no trae `EdDSA` en su registro por defecto (lo
        # clasifica como "no recomendado" para mantener la lista conservadora). Pasarlo acá es
        # además coherente con la regla del módulo: el algoritmo lo decide el código, nunca el
        # token.
        return jwt.encode(
            {"alg": clave.algorithm, "kid": clave.kid},
            claims.model_dump(mode="json"),
            conjunto,
            algorithms=[clave.algorithm],
        )


class JoserfcTokenVerifier:
    """
    Verifica tokens. **El algoritmo lo decide esta clase, nunca el token.**

    Uso::

        verificador = JoserfcTokenVerifier(issuer="https://api.ejemplo.com", key_store=almacen)
        claims = await verificador.verify(token, transport="cookie", expected_typ="at+jwt")
    """

    def __init__(
        self,
        *,
        issuer: str,
        key_store: "AbstractKeyStore",
        clock: "AbstractClock | None" = None,
        allowed_algorithms: t.Sequence[str] | None = None,
        leeway: timedelta = DEFAULT_LEEWAY,
        negative_cache_size: int = 256,
    ) -> None:
        self._issuer = issuer
        self._keys = key_store
        self._allowed = tuple(allowed_algorithms or DEFAULT_ALLOWED_ALGORITHMS)
        self._leeway = leeway
        if clock is None:
            from hexcore.darwin.infrastructure.clock import SystemClock

            clock = SystemClock()
        self._clock = clock

        if any(alg.startswith("HS") for alg in self._allowed):
            # No se prohíbe —hay despliegues de un solo servicio donde HS* es razonable— pero
            # tiene que ser una decisión consciente, porque habilita la confusión de algoritmo
            # si el almacén comparte claves con el asimétrico.
            import warnings

            warnings.warn(
                "La allowlist de algoritmos incluye HS*. Con un almacén que también tenga "
                "claves asimétricas, un atacante puede firmar HS256 usando la clave pública "
                "como secreto HMAC. Mantené los almacenes simétrico y asimétrico separados.",
                stacklevel=2,
            )

        #: `kid` desconocidos ya vistos. Sin esto, un flood de `kid` inventados es un `SELECT`
        #: por petición contra el almacén de claves: un ataque de amplificación gratis.
        self._kid_desconocidos: dict[str, None] = {}
        self._cache_max = negative_cache_size

    async def verify(
        self,
        token: str,
        *,
        transport: "Transport",
        expected_typ: TokenType = "at+jwt",
    ) -> AccessTokenClaims:
        """
        Verifica firma, algoritmo, `kid`, `iss`, `aud`, `typ` y ventana temporal.

        **No verifica revocación**: eso es de `revocation.py`, y es una capa aparte porque
        toca un backend distinto y tiene otra política de fallo (cerrar, no abrir).

        Raises:
            TokenMalformedError: firma inválida, `alg` fuera de la allowlist, `kid` desconocido
                o retirado, `iss` que no coincide, `typ` equivocado, estructura corrupta.
            TokenExpiredError: `exp` pasado, contando `leeway`.
            TokenAudienceMismatchError: el token llegó por otro transporte.
        """
        from joserfc import jwt
        from joserfc.jwk import KeySet

        from hexcore.darwin.infrastructure.keys import RetiredKeyError, UnknownKeyError

        cabecera = self._leer_cabecera(token)

        algoritmo = cabecera.get("alg")
        if algoritmo not in self._allowed:
            # Cubre `alg: none` y cualquier algoritmo que no hayamos autorizado. El mensaje
            # no dice cuáles se aceptan: eso le ahorraría trabajo a quien está probando.
            raise TokenMalformedError(
                f"El algoritmo del token ({algoritmo!r}) no está permitido."
            )

        # `jku`, `x5u` y `jwk` embebidos se ignoran por completo. Honrarlos dejaría que el
        # token elija con qué clave se verifica, que es la vulnerabilidad entera.
        kid = cabecera.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenMalformedError("El token no trae `kid` en la cabecera.")

        if kid in self._kid_desconocidos:
            raise UnknownKeyError(f"kid desconocido: {kid!r}")

        clave = await self._keys.get(kid)
        if clave is None:
            self._recordar_desconocido(kid)
            raise UnknownKeyError(f"kid desconocido: {kid!r}")
        if not clave.can_verify:
            raise RetiredKeyError(
                f"La clave {kid!r} está retirada y ya no verifica tokens."
            )
        if clave.algorithm != algoritmo:
            # El `alg` del token tiene que coincidir con el de la clave que dice usar. Sin
            # este chequeo, un token `HS256` con el `kid` de una clave Ed25519 llega a la
            # verificación y ahí la clave pública se usa como secreto.
            raise TokenMalformedError(
                f"El algoritmo del token ({algoritmo!r}) no coincide con el de la clave "
                f"{kid!r} ({clave.algorithm!r})."
            )

        conjunto = KeySet.import_key_set({"keys": [_jwk(clave.public_key)]})
        try:
            decodificado = jwt.decode(token, conjunto, algorithms=list(self._allowed))
        except Exception as exc:
            raise TokenMalformedError(f"El token no verifica: {type(exc).__name__}") from exc

        try:
            claims = AccessTokenClaims.model_validate(decodificado.claims)
        except Exception as exc:
            raise TokenMalformedError(
                f"Los claims del token no son válidos: {exc}"
            ) from exc

        if claims.iss != self._issuer:
            raise TokenMalformedError("El emisor del token no coincide.")

        if claims.typ != expected_typ:
            # Un refresh presentado donde va un access. Sin este chequeo, el TTL corto del
            # access no sirve para nada: se usa el refresh, que vive treinta días.
            raise TokenMalformedError(
                f"Se esperaba un token de tipo {expected_typ!r} y llegó uno {claims.typ!r}."
            )

        esperado = audience_for(self._issuer, transport)
        if claims.aud != esperado:
            raise TokenAudienceMismatchError(
                "El token se emitió para otro transporte. Una cookie no se puede presentar "
                "como Bearer: esquivaría SameSite y el chequeo anti-CSRF."
            )

        ahora = self._clock.now()
        if claims.is_expired_at(ahora, leeway=self._leeway):
            raise TokenExpiredError("El token venció.")

        nbf = datetime.fromtimestamp(claims.nbf, tz=ahora.tzinfo)
        if ahora + self._leeway < nbf:
            raise TokenMalformedError(
                "El token todavía no es válido (`nbf` en el futuro). Puede ser desfase de "
                "reloj entre el emisor y este proceso."
            )

        return claims

    @staticmethod
    def _leer_cabecera(token: str) -> dict[str, t.Any]:
        """
        Lee la cabecera **sin verificar**, sólo para saber qué clave pedir.

        Nada de lo que sale de acá se confía: el `alg` se compara contra la allowlist y el
        `kid` se usa únicamente como clave de búsqueda.
        """
        import base64
        import json

        try:
            cabecera_b64 = token.split(".", 1)[0]
            relleno = "=" * (-len(cabecera_b64) % 4)
            crudo = base64.urlsafe_b64decode(cabecera_b64 + relleno)
            cabecera = json.loads(crudo)
        except Exception as exc:
            raise TokenMalformedError("La cabecera del token no se puede leer.") from exc

        if not isinstance(cabecera, dict):
            raise TokenMalformedError("La cabecera del token no es un objeto.")
        # Las claves de un objeto JSON son siempre `str`; el narrowing de `isinstance` no lo
        # sabe. Nada de lo que sale de acá se confía igual: el `alg` se compara contra la
        # allowlist y el `kid` sólo se usa como clave de búsqueda.
        return t.cast("dict[str, t.Any]", cabecera)

    def _recordar_desconocido(self, kid: str) -> None:
        """Caché negativa acotada: sin techo, el flood se convierte en consumo de memoria."""
        if len(self._kid_desconocidos) >= self._cache_max:
            self._kid_desconocidos.clear()
        self._kid_desconocidos[kid] = None


def _jwk(serialized: str) -> dict[str, t.Any]:
    import json

    return t.cast("dict[str, t.Any]", json.loads(serialized))
