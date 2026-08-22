"""
Almacenamiento del `state` de OAuth en Beanie. Requiere `[darwin-beanie]`.

Una colección efímera: el `state` vive segundos o minutos entre el redirect al proveedor y el
callback. Acá el índice TTL hace la mayor parte del trabajo que en SQL hacía `delete_expired`.
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

from hexcore.darwin.plugins.oauth.domain import (
    AbstractOAuthStateRepository,
    OAuthState,
)

__all__ = [
    "OAuthStateDocument",
    "BeanieOAuthStateRepository",
    "OAuthStateRepository",
    "OAUTH_DOCUMENTS",
]


class OAuthStateDocument(Document):
    """
    Colección `darwin_oauth_state`: un flujo de autorización en vuelo.

    `state_hash` y no el `state`: el valor viaja por la URL y queda en el historial del navegador y
    en los logs del proveedor. Y `code_verifier_encrypted` cifrado, porque el verificador de PKCE
    **no puede** viajar en la URL — si viajara, PKCE no protegería nada.
    """

    entity_id: UUID = Field(default_factory=uuid4)
    provider_id: str
    state_hash: str
    code_verifier_encrypted: str
    redirect_uri: str
    link_user_id: UUID | None = None
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_oauth_state"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            # Único globalmente: el `state` sale de 32 bytes de aleatoriedad, así que el índice
            # convierte una colisión imposible en un error de base en vez de en dos flujos que se
            # pisan.
            IndexModel([("state_hash", pymongo.ASCENDING)], unique=True),
            # El canje filtra por proveedor **y** hash.
            IndexModel(
                [
                    ("provider_id", pymongo.ASCENDING),
                    ("state_hash", pymongo.ASCENDING),
                ]
            ),
            IndexModel([("expires_at", pymongo.ASCENDING)], expireAfterSeconds=0),
        ]


OAUTH_DOCUMENTS: tuple[type[Document], ...] = (OAuthStateDocument,)


class BeanieOAuthStateRepository(AbstractOAuthStateRepository):
    """`AbstractOAuthStateRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or OAuthStateDocument

    async def add(self, state: OAuthState) -> OAuthState:
        doc = self._doc(
            entity_id=state.id,
            provider_id=state.provider_id,
            state_hash=state.state_hash,
            code_verifier_encrypted=state.code_verifier_encrypted,
            redirect_uri=state.redirect_uri,
            link_user_id=state.link_user_id,
            expires_at=state.expires_at,
        )
        await doc.insert()
        return _a_entidad(doc)

    async def consume(
        self, provider_id: str, state_hash: str, *, at: datetime
    ) -> OAuthState | None:
        """
        Canjea el `state` en un solo `findOneAndUpdate`.

        Los cuatro filtros van en la consulta: proveedor, hash, `consumed_at is None` y el
        vencimiento. Sin la atomicidad, dos callbacks concurrentes con el mismo `code` pasan los
        dos y el `state` deja de ser de un solo uso — que es la mitad de su valor como defensa
        anti-CSRF.
        """
        doc = await self._doc.find_one(
            self._doc.provider_id == provider_id,
            self._doc.state_hash == state_hash,
            self._doc.consumed_at == None,  # noqa: E711
            self._doc.expires_at > at,
        ).update(
            {"$set": {"consumed_at": at, "updated_at": datetime.now(UTC)}},
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return _a_entidad(doc) if doc is not None else None

    async def delete_expired(self, *, before: datetime) -> int:
        """Red de contención sobre el índice TTL. Ver el docstring del módulo."""
        resultado = await self._doc.find(self._doc.expires_at < before).delete()
        return int(getattr(resultado, "deleted_count", 0) or 0)


def _a_entidad(doc: t.Any) -> OAuthState:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return OAuthState(
        id=doc.entity_id,
        provider_id=doc.provider_id,
        state_hash=doc.state_hash,
        code_verifier_encrypted=doc.code_verifier_encrypted,
        redirect_uri=doc.redirect_uri,
        link_user_id=doc.link_user_id,
        expires_at=to_utc(doc.expires_at) or datetime.now(UTC),
        consumed_at=to_utc(doc.consumed_at),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
OAuthStateRepository = BeanieOAuthStateRepository
