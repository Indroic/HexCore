"""
Almacenamiento de `two_factor` en Beanie. Requiere `[darwin-beanie]`.

Un documento y las tres operaciones atómicas del plugin. `consume_step` es la que importa: es la
defensa de replay, y la condición `sign_count < nuevo` va **en el filtro** igual que iba en el
`WHERE` del backend de SQL.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pymongo
from beanie import Document
from beanie.odm.queries.update import UpdateResponse
from pydantic import Field
from pymongo import IndexModel

from hexcore.darwin.plugins.two_factor.domain import (
    AbstractTwoFactorRepository,
    TwoFactor,
)

__all__ = [
    "TwoFactorDocument",
    "BeanieTwoFactorRepository",
    "TwoFactorRepository",
    "TWO_FACTOR_DOCUMENTS",
]


class TwoFactorDocument(Document):
    """
    Colección `darwin_two_factor`.

    No hereda del `BaseDocument` del framework por lo mismo que los del núcleo: traería
    `use_cache = True` —un factor leído del cache diría que sigue confirmado después de
    desactivarlo— y `is_root = True`.

    `secret_encrypted` guarda el secreto cifrado por `SecretBox`, no en claro y no hasheado:
    verificar un código exige **recalcularlo**, así que el secreto tiene que poder recuperarse.
    """

    entity_id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    secret_encrypted: str
    confirmed_at: datetime | None = None
    last_used_step: int | None = None
    failed_attempts: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_two_factor"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            # Único por usuario: dos filas dejarían que el secreto de una inscripción abandonada
            # siga sirviendo para entrar, y ningún flujo lo borraría nunca.
            IndexModel([("user_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("confirmed_at", pymongo.ASCENDING)]),
        ]


TWO_FACTOR_DOCUMENTS: tuple[type[Document], ...] = (TwoFactorDocument,)


class BeanieTwoFactorRepository(AbstractTwoFactorRepository):
    """`AbstractTwoFactorRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or TwoFactorDocument

    async def get_for_user(self, user_id: UUID) -> TwoFactor | None:
        doc = await self._doc.find_one(self._doc.user_id == user_id)
        return _a_entidad(doc) if doc is not None else None

    async def upsert(self, factor: TwoFactor) -> TwoFactor:
        """
        Reemplaza la fila del usuario, o la crea.

        Borra e inserta en vez de actualizar porque una re-inscripción **es** un factor nuevo:
        arrastrar el `last_used_step` o los `failed_attempts` del secreto anterior dejaría al
        usuario nuevo bloqueado por los intentos del viejo.
        """
        await self._doc.find(self._doc.user_id == factor.user_id).delete()
        doc = self._doc(
            entity_id=factor.id,
            user_id=factor.user_id,
            secret_encrypted=factor.secret_encrypted,
            confirmed_at=factor.confirmed_at,
            last_used_step=factor.last_used_step,
            failed_attempts=factor.failed_attempts,
        )
        await doc.insert()
        return _a_entidad(doc)

    async def confirm(
        self, user_id: UUID, *, at: datetime, step: int
    ) -> TwoFactor | None:
        """
        Confirma **sólo si `confirmed_at` es `None`**, en un `findOneAndUpdate`.

        La condición va en el filtro: si ganara la última de dos confirmaciones concurrentes, el
        `last_used_step` guardado sería el del código que perdió y el del que ganó quedaría
        reusable.
        """
        doc = await self._doc.find_one(
            self._doc.user_id == user_id,
            self._doc.confirmed_at == None,  # noqa: E711
        ).update(
            {
                "$set": {
                    "confirmed_at": at,
                    "last_used_step": step,
                    "failed_attempts": 0,
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return _a_entidad(doc) if doc is not None else None

    async def consume_step(
        self, user_id: UUID, *, step: int, after_step: int | None
    ) -> bool:
        """
        Consume el paso TOTP. `True` si esta llamada fue la que lo consumió.

        ⚠️ **La condición va en el filtro y es toda la defensa de replay.** Leer `last_used_step`,
        comparar en Python y después escribir deja la ventana donde dos peticiones con el mismo
        código leen el mismo valor viejo y las dos pasan — que es exactamente el escenario contra
        el que esto existe.

        El caso `after_step is None` exige `last_used_step` nulo, igual que en SQL: un contador que
        **ya avanzó** y vuelve a cero es una regresión, y se rechaza.
        """
        if after_step is None:
            filtros: tuple[t.Any, ...] = (
                self._doc.user_id == user_id,
                self._doc.last_used_step == None,  # noqa: E711
            )
        else:
            filtros = (
                self._doc.user_id == user_id,
                self._doc.last_used_step != None,  # noqa: E711
                self._doc.last_used_step < step,
            )

        doc = await self._doc.find_one(*filtros).update(
            {
                "$set": {
                    "last_used_step": step,
                    "failed_attempts": 0,
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return doc is not None

    async def record_failure(self, user_id: UUID) -> int:
        """Con `$inc`: contar en Python perdería incrementos bajo fuerza bruta concurrente."""
        doc = await self._doc.find_one(self._doc.user_id == user_id).update(
            {"$inc": {"failed_attempts": 1}, "$set": {"updated_at": datetime.now(UTC)}},
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return int(doc.failed_attempts) if doc is not None else 0

    async def reset_failures(self, user_id: UUID) -> None:
        await self._doc.find_one(self._doc.user_id == user_id).update(
            {"$set": {"failed_attempts": 0, "updated_at": datetime.now(UTC)}}
        )

    async def delete_for_user(self, user_id: UUID) -> bool:
        doc = await self._doc.find_one(self._doc.user_id == user_id)
        if doc is None:
            return False
        await doc.delete()
        return True


def _a_entidad(doc: t.Any) -> TwoFactor:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return TwoFactor(
        id=doc.entity_id,
        user_id=doc.user_id,
        secret_encrypted=doc.secret_encrypted,
        confirmed_at=to_utc(doc.confirmed_at),
        last_used_step=doc.last_used_step,
        failed_attempts=doc.failed_attempts,
        created_at=to_utc(doc.created_at) or datetime.now(UTC),
        updated_at=to_utc(doc.updated_at) or datetime.now(UTC),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
TwoFactorRepository = BeanieTwoFactorRepository
