"""
Prerequisito: los constraints tienen que tener nombre determinista.

SQLAlchemy sólo trae `ix` en `naming_convention`, así que uniques, checks, FKs y PKs
quedaban con el nombre que les asignara el backend. Dos consecuencias concretas:

- **SQLite no puede dropear un constraint sin nombre**, así que una migración de bajada que
  lo intente falla y no se puede escribir a mano.
- Los nombres difieren entre backends, así que la misma migración no se comporta igual en
  el SQLite de desarrollo que en el PostgreSQL de producción.

Se declara antes de la primera tabla que use uniques o FKs —las de identidad, que necesitan
`UNIQUE(email)` y `UNIQUE(provider_id, account_id)`— porque agregarla después es en sí una
migración rompedora: hay que renombrar todo constraint existente, y para eso hace falta
poder nombrarlos, que es justamente lo que falta.
"""
from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import (  # noqa: E402
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402

from hexcore.infrastructure.repositories.orms.sqlalchemy import (  # noqa: E402
    NAMING_CONVENTION,
    Base,
)


@pytest.fixture(scope="module")
def tablas():
    """
    Dos tablas de prueba que ejercen los cinco tipos de constraint.

    Se declaran con nombres improbables y se sacan del metadata al terminar, para no
    contaminar `Base.metadata` —que es lo que Alembic compara— en el resto de la suite.
    """

    class _NcPadre(Base):
        __tablename__ = "_nc_padre"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        email: Mapped[str] = mapped_column(String(50))
        edad: Mapped[int] = mapped_column(Integer)
        __table_args__ = (
            UniqueConstraint("email"),
            CheckConstraint("edad >= 0", name="edad_no_negativa"),
        )

    class _NcHijo(Base):
        __tablename__ = "_nc_hijo"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        padre_id: Mapped[int] = mapped_column(ForeignKey("_nc_padre.id"))
        a: Mapped[str] = mapped_column(String(10))
        b: Mapped[str] = mapped_column(String(10))
        __table_args__ = (UniqueConstraint("a", "b"),)

    yield _NcPadre.__table__, _NcHijo.__table__

    Base.metadata.remove(_NcPadre.__table__)
    Base.metadata.remove(_NcHijo.__table__)


def _nombres(tabla, tipo) -> list[str]:
    return sorted(c.name for c in tabla.constraints if isinstance(c, tipo))


def test_la_convencion_cubre_las_cinco_categorias():
    """Sin las cuatro que faltaban, los constraints quedan a merced del backend."""
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}


def test_ix_queda_igual_que_el_default_de_sqlalchemy():
    """
    Deliberado: cambiarlo haría que el próximo `--autogenerate` de todo proyecto existente
    quisiera renombrar cada índice que ya tiene, y no compra nada.
    """
    assert NAMING_CONVENTION["ix"] == "ix_%(column_0_label)s"


def test_las_primary_keys_tienen_nombre(tablas):
    padre, hijo = tablas

    assert padre.primary_key.name == "pk__nc_padre"
    assert hijo.primary_key.name == "pk__nc_hijo"


def test_los_uniques_tienen_nombre(tablas):
    padre, hijo = tablas

    assert _nombres(padre, UniqueConstraint) == ["uq__nc_padre_email"]
    # Multi-columna: `column_0_N_name` incluye todas, así que dos uniques distintos sobre
    # la misma primera columna no colisionan.
    assert _nombres(hijo, UniqueConstraint) == ["uq__nc_hijo_a_b"]


def test_los_checks_tienen_nombre(tablas):
    padre, _ = tablas

    assert _nombres(padre, CheckConstraint) == ["ck__nc_padre_edad_no_negativa"]


def test_las_foreign_keys_tienen_nombre(tablas):
    from sqlalchemy import ForeignKeyConstraint

    _, hijo = tablas

    # Incluye la tabla referida, así que dos FKs a tablas distintas desde la misma columna
    # no colisionan.
    assert _nombres(hijo, ForeignKeyConstraint) == ["fk__nc_hijo_padre_id__nc_padre"]


def test_la_tabla_de_cron_del_framework_recibe_la_convencion():
    """La única tabla que HexCore ya declaraba: su PK estaba sin nombre."""
    import hexcore.infrastructure.cqrs.cron_sql  # noqa: F401

    tabla = Base.metadata.tables["hexcore_cron_jobs"]

    assert tabla.primary_key.name == "pk_hexcore_cron_jobs"


def test_los_nombres_no_dependen_del_backend(tablas):
    """
    El punto de todo esto: el DDL emitido lleva los nombres de la convención, así que la
    migración es idéntica en SQLite y en PostgreSQL.
    """
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateTable

    padre, _ = tablas

    ddl_sqlite = str(CreateTable(padre).compile(dialect=sqlite.dialect()))
    ddl_pg = str(CreateTable(padre).compile(dialect=postgresql.dialect()))

    for ddl in (ddl_sqlite, ddl_pg):
        assert "pk__nc_padre" in ddl
        assert "uq__nc_padre_email" in ddl
        assert "ck__nc_padre_edad_no_negativa" in ddl


def test_la_fachada_sql_expone_la_convencion():
    """Un proyecto que quiera la misma convención en su propio `MetaData` la necesita."""
    import hexcore.sql as sql

    assert sql.NAMING_CONVENTION == NAMING_CONVENTION
    assert "NAMING_CONVENTION" in sql.__all__
