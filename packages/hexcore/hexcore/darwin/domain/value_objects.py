"""
Value objects de identidad: claims, pares de tokens, mail.

`AccessTokenClaims` reemplaza a `hexcore.domain.auth.value_objects.TokenClaims`, que tenía
cuatro defectos y el tercero era descalificante:

===========================  ====================================================
Defecto                      Consecuencia
===========================  ====================================================
`client_id: str` obligatorio No podía representar un token de sesión de primera
                             parte sin inventar un client id de OAuth.
`scopes: List[Enum] = []`    Default mutable compartido, y `Enum` pelado no
                             sobrevive un `model_dump(mode="json")`.
**Sin `sid`**                **La revocación era imposible por construcción**: sin
                             un id de sesión, el token no se puede atar a ninguna
                             fila revocable.
Sin `aud` / `nbf` / `typ`    Sin `aud` no se distingue el transporte, así que una
                             cookie se puede replayear como Bearer y esquivar
                             CSRF. Sin `typ`, un refresh token se puede presentar
                             donde se espera un access token.
===========================  ====================================================

Módulo de dominio puro: stdlib + pydantic. La firma y la verificación son de infraestructura
y viven en `hexcore.darwin.infrastructure.tokens`.
"""
from __future__ import annotations

import typing as t
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "TokenType",
    "Email",
    "AccessTokenClaims",
    "TokenPair",
    "VerificationPurpose",
    "CoreVerificationPurpose",
]

#: `at+jwt` / `rt+jwt` son los media types registrados (RFC 9068 para el access token).
#: Va en el claim `typ` y se verifica: sin esto, un refresh token —que vive mucho más— se
#: puede presentar donde se espera un access token, y el TTL corto deja de servir para algo.
TokenType = t.Literal["at+jwt", "rt+jwt"]

#: Para qué se emitió un token de verificación. Es parte de la clave única junto con el
#: identificador, así que un código de "resetear contraseña" no se puede canjear en el flujo
#: de "verificar mail".
#:
#: Es **abierto** —`str` y no un `Literal`— porque `verification` es la tabla que los plugins
#: reusan en vez de aportar una propia, y un `Literal` no se puede extender desde afuera:
#: enumerar acá `"magic_link"` y `"two_factor"` obligaba al núcleo a conocer por nombre a dos
#: plugins que puede no tener instalados. Cada plugin declara su constante (`MAGIC_LINK_PURPOSE`,
#: `TWO_FACTOR_PURPOSE`) y el núcleo sólo la transporta.
#:
#: Abrirlo no aflojó nada, porque el `Literal` nunca fue lo que daba la garantía: el propósito es
#: parte de la clave de canje, y lo que impide cruzar flujos es que va en el `WHERE` del
#: `consume` —una condición de la consulta, verificada en cada canje— y no una anotación que
#: existe sólo antes de compilar. Donde el valor **sí** entra desde afuera, el tipo sigue
#: cerrado: ver `CoreVerificationPurpose`.
VerificationPurpose = str

#: Los propósitos que el núcleo emite por su cuenta, cerrados.
#:
#: Se usa en la superficie pública —el campo de `IssueVerificationCode`, que llega de un cuerpo
#: HTTP— donde aceptar un `str` cualquiera dejaría pedir un código con `purpose="password_reset"`
#: por el endpoint de verificar mail y canjearlo después en el flujo de reset. Un plugin que
#: emite sus propios códigos expone su propio comando con su propio propósito, y no reusa este.
CoreVerificationPurpose = t.Literal[
    "email_verification",
    "password_reset",
    "otp",
]


class Email(BaseModel):
    """
    Un mail normalizado.

    La normalización es **case-folding del dominio y de la parte local**, más strip. Se
    normaliza porque el mail es la clave única de la tabla de usuarios: sin esto,
    ``Ana@Example.com`` y ``ana@example.com`` crean dos cuentas y el login se vuelve una
    lotería según cómo lo tipeó el usuario.

    No se hace nada más agresivo —ni sacar puntos de gmail, ni cortar el sufijo ``+tag``—
    a propósito: son políticas específicas de cada proveedor, cambian con el tiempo, y
    aplicarlas de prepo impide que alguien use un alias legítimo para separar cuentas.

    Uso::

        Email(value="  Ana@Example.COM ").value   # -> "ana@example.com"
    """

    model_config = ConfigDict(frozen=True)

    value: str = Field(min_length=3, max_length=320)

    @field_validator("value", mode="before")
    @classmethod
    def _normalizar(cls, raw: t.Any) -> t.Any:
        if not isinstance(raw, str):
            return raw
        return raw.strip().casefold()

    @model_validator(mode="after")
    def _tiene_forma_de_mail(self) -> "Email":
        local, _, dominio = self.value.partition("@")
        if not local or not dominio or "." not in dominio or " " in self.value:
            raise ValueError(f"'{self.value}' no tiene forma de dirección de mail.")
        return self

    @property
    def domain(self) -> str:
        return self.value.partition("@")[2]

    def __str__(self) -> str:
        return self.value


class AccessTokenClaims(BaseModel):
    """
    El claim set de un token de Darwin.

    Cada campo está porque su ausencia habilita un ataque concreto:

    - ``sid``: ata el token a una fila de sesión. Sin él **no hay revocación posible**.
    - ``act`` / ``sub``: actor y sujeto por separado, así la impersonación sobrevive al
      token en vez de perderse al re-derivar los claims desde el `sub`.
    - ``aud``: el transporte. Impide replayear una cookie como Bearer.
    - ``typ``: access vs refresh. Impide presentar un refresh donde va un access.
    - ``gen``: la generación del usuario. Permite revocar todas sus sesiones con un solo
      UPDATE, sin importar cuántas tenga.
    - ``nbf``: contra un token emitido con fecha futura por un reloj desincronizado.
    - ``jti``: identidad del token, para la detección de reuso.

    El modelo es `frozen`: un claim set mutable invita a "arreglar" un token verificado en
    memoria, y a partir de ahí lo que valida y lo que se usa dejan de ser lo mismo.
    """

    model_config = ConfigDict(frozen=True)

    iss: str
    #: La cuenta afectada.
    sub: UUID
    #: Quién ejecuta. Igual a `sub` salvo en una impersonación.
    act: UUID
    #: Id de la sesión. **Obligatorio.**
    sid: UUID
    #: El transporte al que está atado (`Transport`, más el issuer si querés namespacearlo).
    aud: str
    typ: TokenType
    #: Generación del usuario, para revocación masiva.
    gen: int = 0
    exp: int
    iat: int
    nbf: int
    jti: UUID = Field(default_factory=uuid4)
    #: `frozenset`: inmutable y serializable, al contrario del `List[Enum]` anterior.
    scopes: frozenset[str] = frozenset()
    #: Si esta sesión es impersonada. Explícito en vez de deducirlo comparando `act` y
    #: `sub`, para que la auditoría no dependa de una inferencia.
    imp: bool = False

    @model_validator(mode="after")
    def _la_ventana_temporal_es_coherente(self) -> "AccessTokenClaims":
        if self.exp <= self.iat:
            raise ValueError("AccessTokenClaims.exp tiene que ser posterior a iat.")
        if self.nbf > self.exp:
            raise ValueError(
                "AccessTokenClaims.nbf es posterior a exp: el token nunca sería válido."
            )
        return self

    @model_validator(mode="after")
    def _la_impersonacion_es_coherente(self) -> "AccessTokenClaims":
        """
        Mismo invariante que `AuthContext`, en el token.

        Si el flag y los ids no concuerdan, algo re-derivó los claims y perdió al actor —
        que es exactamente el modo de falla por el que la impersonación se escapa de la
        auditoría.
        """
        distintos = self.act != self.sub
        if distintos and not self.imp:
            raise ValueError(
                "act difiere de sub pero imp es False: el token perdió la marca de "
                "impersonación y la acción se atribuiría al sujeto."
            )
        if not distintos and self.imp:
            raise ValueError("imp es True pero act y sub son el mismo usuario.")
        return self

    @property
    def is_access(self) -> bool:
        return self.typ == "at+jwt"

    @property
    def expires_at(self) -> datetime:
        from datetime import UTC

        return datetime.fromtimestamp(self.exp, tz=UTC)

    def is_expired_at(self, moment: datetime, *, leeway: timedelta = timedelta()) -> bool:
        """
        Si el token venció a `moment`, con una tolerancia opcional.

        El `leeway` es para el desfase de reloj entre hosts —el que emite y el worker que
        verifica pueden no coincidir— y se aplica **sólo** a la ventana temporal. Nunca a
        la revocación: ahí un margen sería una ventana de uso de un token ya revocado.
        """
        return moment >= self.expires_at + leeway


class TokenPair(BaseModel):
    """
    Lo que devuelve un sign-in o un refresh.

    `refresh_token` es opcional porque el transporte por cookie no lo pone en el cuerpo: va
    en su propia cookie `HttpOnly`. Un cliente Bearer sí lo recibe acá, porque no tiene
    dónde más guardarlo.
    """

    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    #: Segundos de vida del access token, para que el cliente refresque sin decodificarlo.
    expires_in: int
    session_id: UUID
