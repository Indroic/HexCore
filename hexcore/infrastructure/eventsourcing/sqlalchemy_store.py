"""
Adaptadores del event store, los snapshots y los checkpoints sobre SQLAlchemy.

Requiere el extra ``[sql]``.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime

from hexcore.capabilities import require_extra

require_extra("sqlalchemy", para="el event store sobre SQLAlchemy")

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from hexcore.domain.cqrs.resolution import build_fqn  # noqa: E402
from hexcore.domain.events import DomainEvent  # noqa: E402
from hexcore.domain.eventsourcing.exceptions import ConcurrencyError  # noqa: E402
from hexcore.domain.eventsourcing.projections import (  # noqa: E402
    AbstractCheckpointStore,
)
from hexcore.domain.eventsourcing.snapshots import (  # noqa: E402
    AbstractSnapshotStore,
    Snapshot,
)
from hexcore.domain.eventsourcing.store import AbstractEventStore  # noqa: E402
from hexcore.domain.eventsourcing.stored import (  # noqa: E402
    EXPECTED_VERSION_ANY,
    StoredEvent,
)
from hexcore.infrastructure.eventsourcing.memory import stream_type_de  # noqa: E402

from .sqlalchemy_models import (  # noqa: E402
    EventStoreModel,
    ProjectionCheckpointModel,
    SnapshotModel,
)

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from hexcore.domain.cqrs.serializer import AbstractSerializer

__all__ = [
    "SqlAlchemyEventStore",
    "SqlAlchemySnapshotStore",
    "SqlAlchemyCheckpointStore",
]

Orden = t.Literal["sequence", "serialized"]

#: Clave del advisory lock de `ordering="serialized"`. Arbitraria y fija: lo único que
#: importa es que todos los escritores del mismo almacén usen la misma.
_CLAVE_DE_ORDEN = 0x4845584556454E54  # "HEXEVENT" en ASCII


def _ahora() -> datetime:
    return datetime.now(UTC)


def _dialecto_de(session: "AsyncSession") -> str:
    """
    El nombre del dialecto de la sesión.

    `get_bind()` y no el atributo `bind`: el atributo es el que se le pasó al constructor y
    puede no estar —una sesión con binds por tabla no lo tiene—, mientras que `get_bind()` es
    la API que resuelve el engine que se va a usar de verdad. Devuelve `""` si algo falla, y
    con eso los dos llamadores toman el camino conservador: no usar el advisory lock, y no
    usar savepoint.
    """
    try:
        return session.get_bind().dialect.name
    except Exception:
        return ""


class _BaseSql:
    """
    Resuelve la sesión: una inyectada, o una abierta por operación.

    La distinción no es de comodidad. Con una `AsyncSession` inyectada, el adaptador escribe
    en **la transacción del llamador** y no comitea — que es lo que permite que un evento y el
    cambio de negocio que lo produjo entren o no entren juntos. Sin ella, abre una por
    operación con `session_scope()`, como `SqlAlchemyCronJobRepository`, cuyo docstring
    explica el motivo: un proceso de vida larga con una sesión abierta durante horas es
    exactamente cómo se acumulan transacciones idle-in-transaction.
    """

    def __init__(
        self,
        *,
        session: "AsyncSession | None" = None,
        session_scope: t.Callable[[], t.AsyncContextManager["AsyncSession"]] | None = None,
    ) -> None:
        if session is not None and session_scope is not None:
            raise ValueError(
                "Pasá 'session' o 'session_scope', no los dos: con una sesión inyectada el "
                "adaptador escribe en la transacción del llamador y no comitea, y con un "
                "scope abre y cierra la suya. Cuál de los dos comportamientos se espera no "
                "se puede adivinar."
            )
        self._session = session
        self._session_scope = session_scope

    def _abrir(self) -> t.AsyncContextManager["AsyncSession"]:
        if self._session is not None:
            return _SesionPrestada(self._session)
        if self._session_scope is not None:
            return self._session_scope()

        from hexcore.infrastructure.uow.scopes import session_scope

        return session_scope()

    @property
    def _es_prestada(self) -> bool:
        """Si la sesión es del llamador: entonces el commit también es suyo."""
        return self._session is not None

    async def _confirmar(self, session: "AsyncSession") -> None:
        """Comitea sólo si la sesión es nuestra."""
        if not self._es_prestada:
            await session.commit()


class _SesionPrestada:
    """Envuelve una sesión ajena en un context manager que no la cierra."""

    def __init__(self, session: "AsyncSession") -> None:
        self._session = session

    async def __aenter__(self) -> "AsyncSession":
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


class SqlAlchemyEventStore(_BaseSql, AbstractEventStore):
    """
    Event store sobre una tabla append-only.

    ## El hueco de la posición global

    `global_position` es una secuencia, y una secuencia **se toma al insertar, no al
    comitear**. La transacción que reservó la posición 10 puede comitear después de la que
    reservó la 11, así que un lector que ya pasó por la 11 —un proyector con
    `WHERE global_position > checkpoint`— nunca vería la 10. Es el defecto clásico de todo
    event store sobre SQL, y no tiene una solución gratis:

    - `ordering="sequence"` (el default) no hace nada al respecto y es rápido. La entrega
      queda **at-least-once**, y `Projector`/`EventStoreRelay` lo compensan releyendo una
      ventana hacia atrás (`safety_window`) y deduplicando por `event_id`. Es la opción
      correcta mientras las proyecciones sean idempotentes, que es un requisito que ya tienen.
    - `ordering="serialized"` toma un advisory lock de PostgreSQL antes de insertar, así que
      las escrituras se ordenan igual que los commits y no hay huecos. El precio es que
      **todas las escrituras del almacén se serializan**. Sólo funciona en PostgreSQL; en
      otro dialecto falla al construir en vez de aceptar la opción y no cumplirla.
    """

    def __init__(
        self,
        *,
        serializer: "AbstractSerializer | None" = None,
        model: type[t.Any] = EventStoreModel,
        session: "AsyncSession | None" = None,
        session_scope: t.Callable[[], t.AsyncContextManager["AsyncSession"]] | None = None,
        ordering: Orden = "sequence",
    ) -> None:
        _BaseSql.__init__(self, session=session, session_scope=session_scope)
        AbstractEventStore.__init__(self, serializer)

        if ordering not in ("sequence", "serialized"):
            raise ValueError(f"ordering desconocido: {ordering!r}")

        self._model = model
        self._ordering: Orden = ordering

    async def append(
        self,
        stream_id: str,
        events: t.Sequence[DomainEvent],
        *,
        expected_version: int,
        stream_type: str | None = None,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> list[StoredEvent]:
        if not events:
            return []

        categoria = stream_type or stream_type_de(stream_id)

        async with self._abrir() as session:
            if self._ordering == "serialized":
                await self._tomar_lock_de_orden(session)

            version_actual = await self._version(session, stream_id)
            if expected_version != EXPECTED_VERSION_ANY and version_actual != expected_version:
                raise ConcurrencyError(stream_id, expected_version, version_actual)

            momento = _ahora()
            filas = [
                self._model(
                    event_id=evento.event_id,
                    stream_id=stream_id,
                    stream_type=categoria,
                    version=version_actual + desplazamiento,
                    event_type=build_fqn(type(evento)),
                    payload=self._serializer.serialize_envelope(evento, metadata),
                    occurred_on=evento.occurred_on,
                    recorded_at=momento,
                )
                for desplazamiento, evento in enumerate(events, start=1)
            ]

            try:
                if self._es_prestada and self._usa_savepoint(session):
                    # SAVEPOINT. El `flush` dispara el `IntegrityError` del UNIQUE y asigna
                    # las posiciones globales; sin el savepoint, ese error deja la transacción
                    # abortada y el llamador no puede hacer nada salvo tirarla entera —
                    # incluido el cambio de negocio que venía en la misma transacción.
                    async with session.begin_nested():
                        session.add_all(filas)
                        await session.flush()
                else:
                    session.add_all(filas)
                    await session.flush()
            except IntegrityError as error:
                conflicto = self._es_conflicto_de_version(error)
                if not self._es_prestada:
                    # La sesión es nuestra, así que se limpia acá. Sin esto queda abortada y
                    # el `_version()` de la línea siguiente —o cualquier uso posterior—
                    # fallaría con un error que no tiene nada que ver con la causa.
                    await session.rollback()
                if not conflicto:
                    raise
                # La comprobación de más arriba es un TOCTOU: entre el SELECT y el INSERT
                # cabe otra transacción. Quien de verdad impide dos eventos con la misma
                # versión es el UNIQUE(stream_id, version), y esto traduce su error al del
                # dominio.
                actual = await self._version(session, stream_id)
                raise ConcurrencyError(stream_id, expected_version, actual) from error

            escritos = [self._a_stored(fila) for fila in filas]
            await self._confirmar(session)
            return escritos

    async def read_stream(
        self,
        stream_id: str,
        *,
        from_version: int = 1,
        to_version: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        consulta = (
            select(self._model)
            .where(
                self._model.stream_id == stream_id,
                self._model.version >= from_version,
            )
            .order_by(self._model.version.asc())
        )
        if to_version is not None:
            consulta = consulta.where(self._model.version <= to_version)
        if limit is not None:
            consulta = consulta.limit(limit)

        async with self._abrir() as session:
            resultado = await session.execute(consulta)
            return [self._a_stored(fila) for fila in resultado.scalars().all()]

    async def read_all(
        self,
        *,
        from_position: int = 0,
        limit: int = 500,
        stream_types: t.Sequence[str] | None = None,
    ) -> list[StoredEvent]:
        consulta = (
            select(self._model)
            .where(self._model.global_position > from_position)
            .order_by(self._model.global_position.asc())
            .limit(limit)
        )
        if stream_types:
            consulta = consulta.where(self._model.stream_type.in_(list(stream_types)))

        async with self._abrir() as session:
            resultado = await session.execute(consulta)
            return [self._a_stored(fila) for fila in resultado.scalars().all()]

    async def stream_version(self, stream_id: str) -> int:
        async with self._abrir() as session:
            return await self._version(session, stream_id)

    # ── Interno ───────────────────────────────────────────────────────────────
    async def _version(self, session: "AsyncSession", stream_id: str) -> int:
        resultado = await session.execute(
            select(func.coalesce(func.max(self._model.version), 0)).where(
                self._model.stream_id == stream_id
            )
        )
        return int(resultado.scalar_one())

    async def _tomar_lock_de_orden(self, session: "AsyncSession") -> None:
        """
        Serializa las escrituras con un advisory lock de transacción.

        `pg_advisory_xact_lock` y no `pg_advisory_lock`: el de transacción se libera solo al
        comitear o al hacer rollback. El otro hay que soltarlo a mano, y una excepción en el
        medio deja el lock tomado hasta que se cierre la conexión — o sea, el almacén entero
        bloqueado.
        """
        dialecto = _dialecto_de(session)
        if dialecto != "postgresql":
            raise RuntimeError(
                f"ordering='serialized' necesita PostgreSQL y el dialecto es '{dialecto}'. "
                "Se apoya en pg_advisory_xact_lock, que no tiene equivalente portable. Usá "
                "ordering='sequence' con el safety_window del Projector: acepta el hueco y "
                "lo compensa releyendo, en vez de prometer un orden que este motor no da."
            )

        from sqlalchemy import text

        await session.execute(
            text("SELECT pg_advisory_xact_lock(:clave)"), {"clave": _CLAVE_DE_ORDEN}
        )


    @staticmethod
    def _usa_savepoint(session: "AsyncSession") -> bool:
        """
        Si el dialecto soporta SAVEPOINT de forma fiable.

        Sólo importa con una sesión prestada: ahí el `IntegrityError` del UNIQUE dejaría
        abortada la transacción del llamador, y el savepoint es lo que permite recuperarse
        sin arrastrar el cambio de negocio que venía en la misma transacción. Con la sesión
        propia no hace falta: se hace `rollback()` y listo, porque no hay nada más adentro.

        En todos lados sí, **menos en SQLite**. El driver de SQLite de la stdlib (`pysqlite`,
        y `aiosqlite` que lo envuelve) gestiona las transacciones por su cuenta y emite un
        `COMMIT` implícito antes de un `SAVEPOINT`. El efecto es concreto y silencioso: la
        escritura queda confirmada, y el `rollback()` posterior del llamador **no la deshace**
        — o sea, lo contrario de lo que el savepoint venía a garantizar. SQLAlchemy lo
        documenta en "Serializable isolation / Savepoints / Transactional DDL", y arreglarlo
        exige desactivar el BEGIN implícito del driver con dos listeners en el engine: eso es
        del engine de la aplicación, no algo que un adaptador pueda decidirle.

        Sin savepoint se pierde la recuperación fina: un `IntegrityError` deja la transacción
        del llamador abortada. Se acepta porque la alternativa es peor —romper la
        atomicidad—, y porque en SQLite no hay escritura concurrente real de todos modos:
        el motor serializa las escrituras, así que ese `IntegrityError` casi no ocurre.
        """
        return _dialecto_de(session) != "sqlite"

    @staticmethod
    def _es_conflicto_de_version(error: IntegrityError) -> bool:
        """
        Si el `IntegrityError` viene del UNIQUE de versión y no de otra restricción.

        Se mira el texto porque cada driver reporta la violación a su manera y ninguno expone
        el nombre del constraint de forma portable. Ante la duda **no** se traduce: convertir
        cualquier `IntegrityError` en `ConcurrencyError` haría que un llamador reintentara
        para siempre un error que no se arregla reintentando.
        """
        detalle = str(error.orig or error).lower()
        return "stream_version" in detalle or (
            "unique" in detalle and "version" in detalle
        )

    def _a_stored(self, fila: t.Any) -> StoredEvent:
        return StoredEvent(
            stream_id=fila.stream_id,
            stream_type=fila.stream_type,
            version=fila.version,
            global_position=fila.global_position,
            event_id=fila.event_id,
            event_type=fila.event_type,
            payload=dict(fila.payload or {}),
            occurred_on=fila.occurred_on,
            recorded_at=fila.recorded_at,
        )


class SqlAlchemySnapshotStore(_BaseSql, AbstractSnapshotStore):
    """Snapshots sobre SQL, guardando historial por `(stream_id, version)`."""

    def __init__(
        self,
        *,
        model: type[t.Any] = SnapshotModel,
        session: "AsyncSession | None" = None,
        session_scope: t.Callable[[], t.AsyncContextManager["AsyncSession"]] | None = None,
    ) -> None:
        super().__init__(session=session, session_scope=session_scope)
        self._model = model

    async def load(
        self, stream_id: str, *, max_version: int | None = None
    ) -> Snapshot | None:
        consulta = (
            select(self._model)
            .where(self._model.stream_id == stream_id)
            .order_by(self._model.version.desc())
            .limit(1)
        )
        if max_version is not None:
            consulta = consulta.where(self._model.version <= max_version)

        async with self._abrir() as session:
            fila = (await session.execute(consulta)).scalars().first()
            if fila is None:
                return None
            return Snapshot(
                stream_id=fila.stream_id,
                aggregate_type=fila.aggregate_type,
                version=fila.version,
                state=dict(fila.state or {}),
                taken_at=fila.taken_at,
            )

    async def save(self, snapshot: Snapshot) -> None:
        async with self._abrir() as session:
            existente = (
                await session.execute(
                    select(self._model).where(
                        self._model.stream_id == snapshot.stream_id,
                        self._model.version == snapshot.version,
                    )
                )
            ).scalars().first()

            if existente is not None:
                # Idempotente: acumular filas con la misma versión haría crecer el historial
                # sin aportar ningún punto de retroceso nuevo.
                existente.aggregate_type = snapshot.aggregate_type
                existente.state = dict(snapshot.state)
                existente.taken_at = snapshot.taken_at
            else:
                session.add(
                    self._model(
                        stream_id=snapshot.stream_id,
                        aggregate_type=snapshot.aggregate_type,
                        version=snapshot.version,
                        state=dict(snapshot.state),
                        taken_at=snapshot.taken_at,
                    )
                )
            await session.flush()
            await self._confirmar(session)

    async def delete(self, stream_id: str) -> None:
        from sqlalchemy import delete as sql_delete

        async with self._abrir() as session:
            await session.execute(
                sql_delete(self._model).where(self._model.stream_id == stream_id)
            )
            await self._confirmar(session)


class SqlAlchemyCheckpointStore(_BaseSql, AbstractCheckpointStore):
    """Checkpoints sobre SQL, una fila por suscripción."""

    def __init__(
        self,
        *,
        model: type[t.Any] = ProjectionCheckpointModel,
        session: "AsyncSession | None" = None,
        session_scope: t.Callable[[], t.AsyncContextManager["AsyncSession"]] | None = None,
    ) -> None:
        super().__init__(session=session, session_scope=session_scope)
        self._model = model

    async def load(self, subscription: str) -> int:
        async with self._abrir() as session:
            fila = await session.get(self._model, subscription)
            return int(fila.position) if fila is not None else 0

    async def save(self, subscription: str, position: int) -> None:
        async with self._abrir() as session:
            fila = await session.get(self._model, subscription)
            if fila is None:
                session.add(self._model(subscription=subscription, position=position))
            else:
                fila.position = position
                fila.updated_at = _ahora()
            await session.flush()
            await self._confirmar(session)

    async def reset(self, subscription: str) -> None:
        from sqlalchemy import delete as sql_delete

        async with self._abrir() as session:
            await session.execute(
                sql_delete(self._model).where(
                    self._model.subscription == subscription
                )
            )
            await self._confirmar(session)
