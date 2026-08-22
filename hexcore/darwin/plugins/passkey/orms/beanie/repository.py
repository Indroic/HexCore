"""
Almacenamiento de `passkey` en Beanie. Requiere `[darwin-beanie]`.

Dos colecciones —las credenciales viven para siempre, los desafíos treinta segundos— y una
operación que importa: `bump_sign_count`, que es la **única señal de compromiso que WebAuthn da**.
La condición `sign_count < nuevo` va en el filtro, igual que en el `WHERE` del backend de SQL.
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

from hexcore.darwin.plugins.passkey.domain import (
    AbstractPasskeyChallengeRepository,
    AbstractPasskeyRepository,
    ChallengePurpose,
    Passkey,
    PasskeyChallenge,
)

__all__ = [
    "PasskeyDocument",
    "PasskeyChallengeDocument",
    "BeaniePasskeyRepository",
    "BeaniePasskeyChallengeRepository",
    "PasskeyRepository",
    "PasskeyChallengeRepository",
    "PASSKEY_DOCUMENTS",
]


class PasskeyDocument(Document):
    """
    Colección `darwin_passkey`.

    **La clave pública se guarda en claro y eso está bien**: es pública por diseño, y es lo que
    hace a WebAuthn resistente al phishing — un servidor comprometido no entrega nada que sirva
    para autenticarse en otro lado. Es la asimetría deliberada con `two_factor`, donde el secreto
    es compartido y por eso va cifrado.
    """

    entity_id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    credential_id: str
    public_key: str
    sign_count: int = 0
    name: str | None = None
    aaguid: str | None = None
    backed_up: bool = False
    transports: list[str] = Field(default_factory=list)
    last_used_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_passkey"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            # Único **globalmente** y no por usuario: lo genera el autenticador, y la misma
            # credencial en dos cuentas haría ambiguo el login sin usuario declarado —donde se
            # busca sólo por el id.
            IndexModel([("credential_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("user_id", pymongo.ASCENDING)]),
        ]


class PasskeyChallengeDocument(Document):
    """
    Colección `darwin_passkey_challenge`.

    ⚠️ **El desafío se guarda en claro**, y a diferencia del resto de Darwin eso es lo correcto: un
    desafío WebAuthn es un nonce público —viaja al navegador y vuelve— y conocerlo no permite
    autenticarse porque hace falta la clave privada. Hashearlo obligaría a que el
    `expected_challenge` saliera del propio cliente, y la comparación del verificador quedaría
    entre un valor y sí mismo.
    """

    entity_id: UUID = Field(default_factory=uuid4)
    challenge: str
    purpose: str
    #: `None` en el login sin usuario declarado —credenciales descubribles— que es el caso que
    #: obliga a poder canjear el desafío sin saber de quién es.
    user_id: UUID | None = None
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_passkey_challenge"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("challenge", pymongo.ASCENDING)], unique=True),
            IndexModel(
                [("purpose", pymongo.ASCENDING), ("challenge", pymongo.ASCENDING)]
            ),
            IndexModel([("expires_at", pymongo.ASCENDING)], expireAfterSeconds=0),
        ]


PASSKEY_DOCUMENTS: tuple[type[Document], ...] = (
    PasskeyDocument,
    PasskeyChallengeDocument,
)


class BeaniePasskeyRepository(AbstractPasskeyRepository):
    """`AbstractPasskeyRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or PasskeyDocument

    async def add(self, passkey: Passkey) -> Passkey:
        doc = self._doc(
            entity_id=passkey.id,
            user_id=passkey.user_id,
            credential_id=passkey.credential_id,
            public_key=passkey.public_key,
            sign_count=passkey.sign_count,
            name=passkey.name,
            aaguid=passkey.aaguid,
            backed_up=passkey.backed_up,
            transports=list(passkey.transports),
        )
        await doc.insert()
        return _a_passkey(doc)

    async def get_by_credential_id(self, credential_id: str) -> Passkey | None:
        doc = await self._doc.find_one(self._doc.credential_id == credential_id)
        return _a_passkey(doc) if doc is not None else None

    async def list_for_user(self, user_id: UUID) -> list[Passkey]:
        docs = await self._doc.find(self._doc.user_id == user_id).to_list()
        return [_a_passkey(d) for d in sorted(docs, key=lambda d: d.created_at)]

    async def bump_sign_count(
        self, credential_id: str, *, new_count: int, at: datetime
    ) -> bool:
        """
        Sube el contador **sólo si el nuevo es estrictamente mayor**. `False` si no subió.

        ⚠️ **Es la única señal de compromiso que WebAuthn da**: un contador que no avanza significa
        autenticador clonado o aserción replayeada. La condición va en el filtro porque con
        leer-comparar-escribir dos aserciones concurrentes con el mismo contador leen el mismo
        valor viejo y las dos pasan.

        El caso `new_count == 0` se acepta como "este autenticador no lleva contador" —varias
        llaves y varios navegadores no lo incrementan— pero se exige que el guardado también sea
        0: si alguna vez avanzó, un 0 ahora es una regresión.
        """
        if new_count == 0:
            filtros: tuple[t.Any, ...] = (
                self._doc.credential_id == credential_id,
                self._doc.sign_count == 0,
            )
            cambios: dict[str, t.Any] = {"last_used_at": at}
        else:
            filtros = (
                self._doc.credential_id == credential_id,
                self._doc.sign_count < new_count,
            )
            cambios = {"sign_count": new_count, "last_used_at": at}

        doc = await self._doc.find_one(*filtros).update(
            {"$set": {**cambios, "updated_at": datetime.now(UTC)}},
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return doc is not None

    async def delete(self, passkey_id: UUID) -> bool:
        doc = await self._doc.find_one(self._doc.entity_id == passkey_id)
        if doc is None:
            return False
        await doc.delete()
        return True


class BeaniePasskeyChallengeRepository(AbstractPasskeyChallengeRepository):
    """`AbstractPasskeyChallengeRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or PasskeyChallengeDocument

    async def add(self, challenge: PasskeyChallenge) -> PasskeyChallenge:
        doc = self._doc(
            entity_id=challenge.id,
            challenge=challenge.challenge,
            purpose=challenge.purpose,
            user_id=challenge.user_id,
            expires_at=challenge.expires_at,
        )
        await doc.insert()
        return _a_challenge(doc)

    async def consume(
        self, purpose: ChallengePurpose, challenge: str, *, at: datetime
    ) -> PasskeyChallenge | None:
        """
        Canjea el desafío en un solo `findOneAndUpdate`.

        Filtra por `purpose`: un desafío de registro no se canjea autenticando, que si no sería una
        forma de saltear la verificación de la firma sobre el `clientDataJSON` correcto.
        """
        doc = await self._doc.find_one(
            self._doc.challenge == challenge,
            self._doc.purpose == purpose,
            self._doc.consumed_at == None,  # noqa: E711
            self._doc.expires_at > at,
        ).update(
            {"$set": {"consumed_at": at, "updated_at": datetime.now(UTC)}},
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return _a_challenge(doc) if doc is not None else None

    async def delete_expired(self, *, before: datetime) -> int:
        resultado = await self._doc.find(self._doc.expires_at < before).delete()
        return int(getattr(resultado, "deleted_count", 0) or 0)


def _a_passkey(doc: t.Any) -> Passkey:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return Passkey(
        id=doc.entity_id,
        user_id=doc.user_id,
        credential_id=doc.credential_id,
        public_key=doc.public_key,
        sign_count=doc.sign_count,
        name=doc.name,
        aaguid=doc.aaguid,
        backed_up=doc.backed_up,
        transports=tuple(doc.transports or ()),
        last_used_at=to_utc(doc.last_used_at),
        created_at=to_utc(doc.created_at),
    )


def _a_challenge(doc: t.Any) -> PasskeyChallenge:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return PasskeyChallenge(
        id=doc.entity_id,
        challenge=doc.challenge,
        purpose=t.cast(ChallengePurpose, doc.purpose),
        user_id=doc.user_id,
        expires_at=to_utc(doc.expires_at) or datetime.now(UTC),
        consumed_at=to_utc(doc.consumed_at),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
PasskeyRepository = BeaniePasskeyRepository
PasskeyChallengeRepository = BeaniePasskeyChallengeRepository
