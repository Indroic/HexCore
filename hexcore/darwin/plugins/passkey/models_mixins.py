"""
Los mixins de `passkey`. **Importarlos no registra ninguna tabla.**

Dos tablas y la razón de que sean dos: las credenciales viven para siempre y los desafíos viven
treinta segundos. Meterlos juntos daría una tabla donde el 99% de las filas son basura de un
minuto atrás, y el barrido periódico tendría que distinguir por una columna en vez de por tabla.

Igual que con los mixins del núcleo, el consumidor declara las clases concretas en su propio
paquete ``models/``: un `--autogenerate` que no ve la tabla le emite ``op.drop_table``, y acá eso
sería el segundo factor de todos los usuarios.

Uso, en el paquete del consumidor::

    # myapp/models/identity.py
    from hexcore.darwin.plugins.passkey import PasskeyChallengeMixin, PasskeyMixin
    from hexcore.sql import Base

    class Passkey(PasskeyMixin, Base):
        __tablename__ = "darwin_passkey"

    class PasskeyChallenge(PasskeyChallengeMixin, Base):
        __tablename__ = "darwin_passkey_challenge"
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from hexcore.darwin.infrastructure.models_mixins import (
    DEFAULT_USER_TABLE,
    TimestampMixin,
)

__all__ = [
    "DEFAULT_PASSKEY_TABLE",
    "DEFAULT_PASSKEY_CHALLENGE_TABLE",
    "PasskeyMixin",
    "PasskeyChallengeMixin",
]

DEFAULT_PASSKEY_TABLE = "darwin_passkey"
DEFAULT_PASSKEY_CHALLENGE_TABLE = "darwin_passkey_challenge"


class PasskeyMixin(TimestampMixin):
    """
    Columnas de `passkey`: una credencial WebAuthn registrada.

    **La clave pública se guarda en claro y eso está bien.** Es pública por diseño, y es lo que
    hace a WebAuthn resistente al phishing: un servidor comprometido no entrega nada que sirva
    para autenticarse en otro lado. Es la asimetría exacta con `two_factor`, donde el secreto es
    compartido y por eso va cifrado.
    """

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker sepa que
    #: existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones `ClassVar`.
    __tablename__: t.ClassVar[str]

    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def user_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        """De quién es. `CASCADE`: borrar la cuenta borra sus credenciales."""
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    #: El `credentialId` en base64url. `String(512)` porque la spec no le pone techo y los
    #: autenticadores que envuelven estado en el id llegan a varios cientos de bytes.
    credential_id: Mapped[str] = mapped_column(String(512), nullable=False)

    #: La clave pública en CBOR/COSE, base64url. `Text`: el largo depende del algoritmo, y un
    #: `VARCHAR` corto la truncaría en silencio en MySQL — con el síntoma de que la credencial
    #: registra bien y no autentica nunca.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)

    #: El contador de firmas. `BigInteger` porque la spec lo define de 32 bits sin signo, que no
    #: entra en el `Integer` con signo de algunos backends.
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    #: El nombre que le puso el usuario. Es lo único que le permite distinguir entre cinco
    #: credenciales al momento de borrar una.
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    aaguid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backed_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    transports: Mapped[t.Any] = mapped_column(JSON, nullable=False, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            # `credential_id` es único **globalmente** y no por usuario: lo genera el
            # autenticador, y la misma credencial en dos cuentas haría que el login sin usuario
            # declarado —donde se busca sólo por el id— fuera ambiguo.
            UniqueConstraint("credential_id", name=f"uq_{nombre}_credential_id"),
            Index(f"ix_{nombre}_user_id", "user_id"),
        )


class PasskeyChallengeMixin(TimestampMixin):
    """
    Columnas de `passkey_challenge`: un desafío en vuelo.

    ⚠️ **Se guarda en claro, y a diferencia del resto de Darwin eso es lo correcto.** Un desafío
    WebAuthn es un nonce público: viaja al navegador y vuelve, y conocerlo no permite autenticarse
    porque hace falta la clave privada del autenticador. No es un token de sesión.

    Y guardarlo hasheado sería peor que innecesario: el `expected_challenge` que el verificador
    compara tendría que salir del `clientDataJSON` del propio cliente —el hash sólo serviría para
    encontrar la fila— y la comparación quedaría entre un valor y sí mismo. Sigue siendo sólida,
    pero es circular de leer, y un chequeo de seguridad que hay que razonar dos veces para ver que
    sirve es un chequeo que alguien va a "simplificar".
    """

    __tablename__: t.ClassVar[str]
    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    #: El desafío en base64url, en claro. Ver el docstring de la clase.
    challenge: Mapped[str] = mapped_column(String(128), nullable=False)

    #: `register` o `authenticate`. Parte de la clave de canje: un desafío de registro no se
    #: canjea autenticando.
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)

    @declared_attr
    def user_id(cls) -> Mapped[PythonUUID | None]:  # noqa: N805
        """
        A qué usuario, si se sabe.

        `NULL` en el login sin usuario declarado —el flujo con credenciales descubribles, donde el
        navegador elige y el servidor todavía no sabe quién es—. Es exactamente el caso que
        obliga a que el desafío se pueda canjear sin conocer al dueño.
        """
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=True,
        )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            UniqueConstraint("challenge", name=f"uq_{nombre}_challenge"),
            Index(f"ix_{nombre}_expires_at", "expires_at"),
        )
