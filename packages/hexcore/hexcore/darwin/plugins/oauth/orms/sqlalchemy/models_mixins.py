"""
El mixin de `oauth`: la tabla del `state` en vuelo. **Importarlo no registra ninguna tabla.**

**Por qué una tabla propia y no `verification`.** El resto de los flujos de un solo uso reusan
`verification`, y `magic_link` es el ejemplo. Acá no alcanza: un `state` de OAuth tiene que
guardar el `code_verifier` de PKCE, el `redirect_uri` con el que se inició y —si es una
vinculación— a qué usuario se está vinculando. `verification` tiene un `value_hash` y nada más.

Y el `code_verifier` **no puede** viajar en el `state`, que es lo que permitiría meterlo en la
tabla genérica: el `state` va en la URL de autorización, y un verificador en la URL anula PKCE
por completo — cualquiera que vea esa URL en el historial, en un log de proxy o en un `Referer`
puede canjear el código. Guardarlo del lado del servidor es toda la protección.

El consumidor declara la clase concreta en su paquete ``models/``, igual que con las del núcleo y
por el mismo motivo: un `--autogenerate` que no ve la tabla le emite ``op.drop_table``.

Uso, en el paquete del consumidor::

    # myapp/models/identity.py
    from hexcore.darwin.plugins.oauth import OAuthStateMixin
    from hexcore.sql import Base

    class OAuthState(OAuthStateMixin, Base):
        __tablename__ = "darwin_oauth_state"
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins import (
    DEFAULT_USER_TABLE,
    TimestampMixin,
)

__all__ = ["DEFAULT_OAUTH_STATE_TABLE", "OAuthStateMixin"]

DEFAULT_OAUTH_STATE_TABLE = "darwin_oauth_state"


class OAuthStateMixin(TimestampMixin):
    """
    Columnas de `oauth_state`: un flujo de autorización en vuelo.

    Vive segundos o minutos: se crea al redirigir al proveedor y se consume en el callback.
    `expires_at` está indexado justamente para poder barrer las que quedaron —el usuario que
    cerró la pestaña en la pantalla de consentimiento— sin escanear la tabla.
    """

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker sepa
    #: que existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones `ClassVar`.
    __tablename__: t.ClassVar[str]

    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    #: A qué proveedor se inició el flujo. Es parte de la clave de canje: un `state` emitido
    #: para Google no se puede canjear en el callback de GitHub.
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: **SHA-256 del `state`, no el `state`.** El valor viaja por la URL y queda en el historial
    #: del navegador y en los logs del proveedor; un dump de esta tabla no debería sumar la
    #: capacidad de completar un flujo ajeno.
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: El `code_verifier` de PKCE, **cifrado**. Ver el docstring del módulo: no puede viajar en
    #: la URL, y en claro un dump permitiría canjear un código interceptado.
    code_verifier_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    #: El `redirect_uri` con el que se inició. Se vuelve a mandar en el canje —el proveedor
    #: exige que coincida— y se compara con el del callback: sin eso, un callback en otra URI
    #: pasaría igual y el código terminaría canjeándose para un destino que nadie autorizó.
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)

    @declared_attr
    def link_user_id(cls) -> Mapped[PythonUUID | None]:  # noqa: N805
        """
        A qué usuario se está vinculando, si el flujo es de vinculación y no de login.

        Se fija **al iniciar** y no se toma del callback: el callback lo controla en parte quien
        maneja el navegador, y aceptar de ahí a quién vincular dejaría vincular una identidad
        propia a la cuenta de otro.
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
            # Un `state` es único globalmente: sale de 32 bytes de aleatoriedad. El `UNIQUE`
            # convierte una colisión imposible en un error de base en vez de en dos flujos que
            # se pisan.
            UniqueConstraint("state_hash", name=f"uq_{nombre}_state_hash"),
            Index(f"ix_{nombre}_expires_at", "expires_at"),
        )
