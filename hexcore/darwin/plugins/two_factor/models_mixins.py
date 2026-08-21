"""
El mixin de `two_factor`. **Importar este módulo no registra ninguna tabla.**

Misma propiedad y mismo motivo que `hexcore.darwin.infrastructure.models_mixins`: el plugin
shippea el mixin y **el consumidor** declara la clase concreta en su propio paquete ``models/``,
donde `import_all_models` la ve y `alembic revision --autogenerate` por lo tanto también. Una
tabla del framework que se registra sola pero que el `env.py` del consumidor no importa recibe
un ``op.drop_table`` en la primera migración de rutina — y acá eso es el segundo factor de
todos los usuarios.

Uso, en el paquete del consumidor::

    # myapp/models/identity.py
    from hexcore.darwin.plugins.two_factor import TwoFactorMixin
    from hexcore.sql import Base

    class TwoFactor(TwoFactorMixin, Base):
        __tablename__ = "darwin_two_factor"
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from hexcore.darwin.infrastructure.models_mixins import (
    DEFAULT_USER_TABLE,
    TimestampMixin,
)

__all__ = ["DEFAULT_TWO_FACTOR_TABLE", "TwoFactorMixin"]

DEFAULT_TWO_FACTOR_TABLE = "darwin_two_factor"


class TwoFactorMixin(TimestampMixin):
    """
    Columnas de `two_factor`: el segundo factor TOTP de un usuario.

    Una fila por usuario (`UNIQUE` sobre `user_id`), y esa restricción no es cosmética: dos
    filas dejarían que un secreto viejo —de una inscripción abandonada— siga sirviendo para
    entrar, y ningún flujo lo borraría nunca.

    Tres decisiones que importan:

    1. **`secret_encrypted` guarda el secreto cifrado, no en claro y no hasheado.** Hasheado no
       serviría: para verificar un código hay que **recalcularlo**, así que el secreto tiene que
       poder recuperarse. Se cifra con una clave derivada de `IdentityConfig.secret_key`, así
       que un dump de la base sin la clave de la aplicación no permite generar códigos. Better
       Auth lo guarda en claro.
    2. **`confirmed_at` decide si el 2FA está activo.** Una fila sin confirmar existe pero no
       exige nada: inscribir y activar en el mismo paso deja afuera para siempre al usuario que
       guardó el secreto mal, y recuperarlo requiere soporte humano.
    3. **`last_used_step` es la defensa de replay.** Un código TOTP vale hasta 90 segundos con
       la ventana por default, así que quien lo lee por encima del hombro o lo saca de un
       formulario de phishing lo puede volver a usar. Guardar el paso consumido convierte "es
       válido" en "es válido y no se usó".
    """

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker sepa
    #: que existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones `ClassVar`.
    __tablename__: t.ClassVar[str]

    #: La tabla de usuarios a la que apunta la FK. Se sobreescribe en la clase concreta si
    #: renombrás `user`, porque una FK no se puede derivar sola.
    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def user_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        """De quién es el segundo factor. `CASCADE`: borrar la cuenta borra su factor."""
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    #: El secreto TOTP cifrado. `Text` y no `String(n)`: el largo del texto cifrado depende del
    #: formato, y un `VARCHAR` corto lo truncaría en silencio en MySQL.
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    #: Cuándo se confirmó con un código válido. `NULL` = inscripto pero **inactivo**.
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: El último paso TOTP consumido. `BigInteger` porque el paso es epoch/30: ya pasó de los
    #: 2^31 que aguanta un `Integer` en 2038, y esa fecha está dentro del horizonte de una
    #: migración de datos.
    last_used_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: Intentos fallidos seguidos. Le pone techo a la fuerza bruta sobre 10^6 combinaciones,
    #: que sin límite se agotan en minutos.
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            UniqueConstraint("user_id", name=f"uq_{nombre}_user_id"),
            Index(f"ix_{nombre}_confirmed_at", "confirmed_at"),
        )
