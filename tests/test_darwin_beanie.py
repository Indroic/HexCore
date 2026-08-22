"""
El backend de Beanie/MongoDB de Darwin.

**Dos tiers, y la razón es honesta.** Lo que hay que probar de estos adaptadores es que las
operaciones de seguridad sean **atómicas**, y eso sólo lo exhibe un servidor real. Pero un backend
sin ningún test hasta que alguien levante un Mongo tampoco es aceptable. Así que:

1. **Sin servidor** — se asevera el **documento de filtro exacto** que se le manda a Mongo. Es el
   test que atrapa la regresión que importa: alguien que "simplifica" el
   `find_one(...).update(...)` a un `doc = await find_one(...)` / `doc.save()` deja el código
   leyéndose igual de bien y rompe la atomicidad. El filtro no miente.
2. **Con servidor** (`-m mongo`, con `HEXCORE_TEST_MONGO_URI`) — el flujo completo y las carreras
   de verdad, con `asyncio.gather`.

El tier 2 está **deseleccionado y no salteado**: el gate de CI exige cero SKIPPED en la corrida
normal, y un salteo por falta de servidor rompería esa señal.
"""
from __future__ import annotations

import os
import typing as t
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("beanie")

from hexcore.darwin.domain.entities import Verification  # noqa: E402
from hexcore.darwin.infrastructure.orms.beanie import repositories as repos  # noqa: E402
from hexcore.darwin.infrastructure.orms.beanie.documents import (  # noqa: E402
    IDENTITY_DOCUMENTS,
    AccountDocument,
    AuditLogDocument,
    JwksDocument,
    SessionDocument,
    UserDocument,
    VerificationDocument,
)
from hexcore.darwin.infrastructure.orms.beanie.schema import (  # noqa: E402
    identity_documents,
)

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
MONGO_URI = os.getenv("HEXCORE_TEST_MONGO_URI", "")


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Los documentos ────────────────────────────────────────────────────────────
class TestDocumentos:
    def test_son_seis_con_prefijo(self):
        nombres = [d.Settings.name for d in IDENTITY_DOCUMENTS]

        assert len(nombres) == 6
        assert all(n.startswith("darwin_") for n in nombres), nombres

    def test_ninguno_usa_cache(self):
        """
        ⚠️ **Es un chequeo de seguridad, no de rendimiento.** El `BaseDocument` del framework trae
        `use_cache = True` con expiración de 10 minutos: un documento de `session` leído del cache
        diría que la sesión sigue viva hasta diez minutos después de revocarla — o sea que cerrar
        sesión no tendría efecto.
        """
        for documento in IDENTITY_DOCUMENTS:
            assert getattr(documento.Settings, "use_cache", False) is False, (
                f"{documento.__name__} tiene el cache prendido"
            )

    def test_ninguno_es_root_de_herencia(self):
        """
        `is_root = True` en Beanie significa herencia de **una sola colección**: usuarios,
        sesiones y cuentas terminarían todos en el mismo `collection`.
        """
        for documento in IDENTITY_DOCUMENTS:
            assert getattr(documento.Settings, "is_root", False) is False, (
                f"{documento.__name__} es root de una jerarquía de una colección"
            )

    def test_no_heredan_del_base_document_del_framework(self):
        """
        La regla análoga a "no heredar `BaseModel[T]`" del backend de SQL, y con un motivo más
        filoso: heredarlo traería el cache y la herencia de una colección.
        """
        pytest.importorskip("sqlalchemy")  # `BaseDocument` vive junto a la capa de repos
        from hexcore.infrastructure.repositories.orms.beanie import BaseDocument

        for documento in IDENTITY_DOCUMENTS:
            assert not issubclass(documento, BaseDocument), documento.__name__

    def test_cada_uno_tiene_su_coleccion(self):
        nombres = [d.Settings.name for d in IDENTITY_DOCUMENTS]

        assert len(set(nombres)) == len(nombres), f"colecciones repetidas: {nombres}"

    @pytest.mark.parametrize(
        "documento, campo",
        [
            (UserDocument, "email"),
            (SessionDocument, "token_hash"),
            (JwksDocument, "kid"),
        ],
    )
    def test_los_campos_de_busqueda_son_unicos(self, documento, campo):
        """
        Sin el índice único, dos altas concurrentes con el mismo mail crean dos usuarios y el
        login pasa a ser una lotería según cuál devuelva la consulta.
        """
        # `IndexModel.document["key"]` es un dict `{campo: direccion}`, no una lista de pares.
        unicos = {
            tuple(idx.document["key"].keys())
            for idx in documento.Settings.indexes
            if idx.document.get("unique")
        }

        assert (campo,) in unicos, f"{documento.__name__}.{campo} no es único"

    def test_las_colecciones_efimeras_tienen_ttl(self):
        """
        Sesiones y verificaciones vencen: con el índice TTL, Mongo las borra solo. Es una ventaja
        real sobre el backend de SQL, donde hace falta el cron del reaper.
        """
        for documento in (SessionDocument, VerificationDocument):
            con_ttl = [
                idx
                for idx in documento.Settings.indexes
                if "expireAfterSeconds" in idx.document
            ]
            assert con_ttl, f"{documento.__name__} no tiene índice TTL"

    def test_la_auditoria_no_tiene_ttl(self):
        """
        ⚠️ A propósito: el valor entero de la auditoría es poder responder qué pasó seis meses
        después. La retención es una decisión del consumidor y de su regulación.
        """
        con_ttl = [
            idx
            for idx in AuditLogDocument.Settings.indexes
            if "expireAfterSeconds" in idx.document
        ]

        assert not con_ttl, "la auditoría no puede vencer sola"

    def test_la_cuenta_del_proveedor_es_unica(self):
        """
        El constraint que hace segura la vinculación OAuth: la misma cuenta de Google no se puede
        vincular a dos usuarios.
        """
        compuestos = {
            tuple(idx.document["key"].keys())
            for idx in AccountDocument.Settings.indexes
            if idx.document.get("unique")
        }

        assert ("provider_id", "account_id") in compuestos

    def test_el_usuario_se_puede_extender_subclaseando(self):
        """
        En Mongo los documentos son concretos y no mixins: no hay `--autogenerate` que engañar, así
        que la ceremonia del mixin no compraría nada.
        """

        class MiUsuario(UserDocument):
            plan: str = "free"

        assert "plan" in MiUsuario.model_fields
        assert "email" in MiUsuario.model_fields

    def test_identity_documents_acepta_los_propios(self):
        class MiUsuario(UserDocument):
            pass

        assert identity_documents([MiUsuario]) == [MiUsuario]


# ── El contrato del backend ───────────────────────────────────────────────────
class TestContrato:
    def test_expone_los_cinco_nombres_neutros(self):
        for nombre in (
            "UserRepository",
            "SessionRepository",
            "AccountRepository",
            "VerificationRepository",
            "AuditSink",
        ):
            assert hasattr(repos, nombre), nombre

    def test_los_alias_apuntan_a_las_clases_beanie(self):
        assert repos.UserRepository is repos.BeanieUserRepository
        assert repos.AuditSink is repos.BeanieAuditSink

    def test_el_repositorio_de_usuarios_acepta_model_como_alias(self):
        """
        El contenedor le pasa `IdentityConfig.user_model` sin saber en qué backend está. Aceptar
        `model=` como alias de `document=` evita un `if backend == ...` en el contenedor.
        """

        class MiUsuario(UserDocument):
            pass

        assert repos.BeanieUserRepository(model=MiUsuario)._doc is MiUsuario
        assert repos.BeanieUserRepository(document=MiUsuario)._doc is MiUsuario


# ── Los filtros: el test que atrapa la regresión que importa ──────────────────
class _Campo:
    """
    Un campo de documento falso: cualquier comparación devuelve una condición registrable.

    Hace falta porque los adaptadores escriben `self._doc.expires_at > at` y
    `self._doc.consumed_at == None`, y un `MagicMock` no soporta `>` contra un `datetime` — el
    test fallaría por el doble y no por el código.
    """

    __slots__ = ("nombre",)

    def __init__(self, nombre: str) -> None:
        self.nombre = nombre

    def _condicion(self, operador: str, valor: t.Any) -> tuple[str, str, t.Any]:
        return (self.nombre, operador, valor)

    def __eq__(self, otro: t.Any) -> t.Any:  # type: ignore[override]
        return self._condicion("==", otro)

    def __ne__(self, otro: t.Any) -> t.Any:  # type: ignore[override]
        return self._condicion("!=", otro)

    def __gt__(self, otro: t.Any) -> t.Any:
        return self._condicion(">", otro)

    def __lt__(self, otro: t.Any) -> t.Any:
        return self._condicion("<", otro)

    def __hash__(self) -> int:
        return hash(self.nombre)


class _Espia:
    """
    Un doble de `Document` que registra con qué filtro y qué update lo llamaron.

    No simula Mongo: **sólo captura la consulta**. Es todo lo que hace falta para el chequeo que
    importa —que la condición esté en el filtro y no en Python— y no pretende probar semántica,
    que es lo que hace el tier con servidor.
    """

    #: Los campos que los adaptadores comparan. Se declaran en vez de usar un `__getattr__`
    #: genérico para que un typo en un adaptador falle acá en vez de crear una condición sobre un
    #: campo inexistente.
    CAMPOS = (
        "entity_id",
        "email",
        "token_hash",
        "family_id",
        "actor_user_id",
        "consumed_at",
        "revoked_at",
        "expires_at",
        "identifier",
        "purpose",
        "value_hash",
        "user_id",
        "provider_id",
        "account_id",
    )

    def __init__(self, devuelve: t.Any = None) -> None:
        self.devuelve = devuelve
        self.filtros: list[tuple[t.Any, ...]] = []
        self.updates: list[t.Any] = []
        self.response_types: list[t.Any] = []

        for campo in self.CAMPOS:
            setattr(self, campo, _Campo(campo))

    # ── La API de Beanie que los adaptadores usan ─────────────────────────
    def find_one(self, *filtros: t.Any) -> "_Espia":
        self.filtros.append(filtros)
        return self

    def find(self, *filtros: t.Any) -> "_Espia":
        self.filtros.append(filtros)
        return self

    async def update(
        self, operacion: t.Any, *, response_type: t.Any = None, **_: t.Any
    ) -> t.Any:
        self.updates.append(operacion)
        self.response_types.append(response_type)
        return self.devuelve

    async def delete(self, **_: t.Any) -> t.Any:
        return self.devuelve


class TestLosFiltrosSonAtomicos:
    """
    ⚠️ **El grupo de tests que importa de este archivo.**

    Cada operación de seguridad tiene que ser **un** `findOneAndUpdate` con la condición en el
    filtro. La regresión que estos tests atrapan es la más fácil de escribir por accidente en
    Mongo:

        doc = await SessionDocument.find_one(entity_id == sid)   # leer
        doc.consumed_at = ahora                                  # comprobar en Python
        await doc.save()                                         # escribir

    Se lee perfectamente natural, pasa cualquier test secuencial, y deja que dos rotaciones
    concurrentes con el mismo token pasen las dos — o sea que la detección de reuso, el único
    mecanismo que detecta un refresh robado, no dispare nunca.
    """

    @pytest.mark.anyio
    async def test_consume_for_rotation_filtra_por_consumed_at(self):
        espia = _Espia(devuelve=None)
        repo = repos.BeanieSessionRepository(document=espia)  # type: ignore[arg-type]

        resultado = await repo.consume_for_rotation(uuid4(), at=AHORA)

        assert resultado is None, "sin documento devuelto, la sesión ya estaba consumida"
        assert len(espia.filtros) == 1, "una sola consulta, no leer-y-después-escribir"
        assert len(espia.filtros[0]) == 2, (
            "dos condiciones: el id y `consumed_at is None`"
        )
        assert espia.updates == [
            {"$set": {"consumed_at": AHORA, "updated_at": espia.updates[0]["$set"]["updated_at"]}}
        ]

    @pytest.mark.anyio
    async def test_consume_for_rotation_pide_el_documento_nuevo(self):
        """
        `NEW_DOCUMENT` y no `UPDATE_RESULT`: el llamador necesita la sesión consumida para rotar,
        y pedir el resultado y después leer de nuevo abriría exactamente la ventana que el
        `findOneAndUpdate` cierra.
        """
        from beanie.odm.queries.update import UpdateResponse

        espia = _Espia()
        await repos.BeanieSessionRepository(
            document=espia  # type: ignore[arg-type]
        ).consume_for_rotation(uuid4(), at=AHORA)

        assert espia.response_types == [UpdateResponse.NEW_DOCUMENT]

    @pytest.mark.anyio
    async def test_consume_de_verificacion_filtra_por_los_cinco(self):
        """
        Identificador, propósito, hash, `consumed_at is None` y el vencimiento. Filtrar por
        `purpose` es lo que impide canjear un código de reset en el flujo de verificar el mail.
        """
        espia = _Espia()
        repo = repos.BeanieVerificationRepository(document=espia)  # type: ignore[arg-type]

        await repo.consume("ana@ejemplo.com", "magic_link", "h", at=AHORA)

        assert len(espia.filtros) == 1
        assert len(espia.filtros[0]) == 5, (
            f"esperaba cinco condiciones, hubo {len(espia.filtros[0])}"
        )

    @pytest.mark.anyio
    async def test_bump_token_generation_usa_inc(self):
        """
        ⚠️ `$inc` y no leer-sumar-escribir. Con dos revocaciones masivas concurrentes, la versión
        en Python sube una sola generación y la mitad de los tokens sigue valiendo.
        """
        espia = _Espia(devuelve=MagicMock(token_generation=7))
        repo = repos.BeanieUserRepository(document=espia)  # type: ignore[arg-type]

        nueva = await repo.bump_token_generation(uuid4())

        assert nueva == 7
        assert espia.updates[0]["$inc"] == {"token_generation": 1}
        assert "$set" not in espia.updates[0] or "token_generation" not in (
            espia.updates[0]["$set"]
        ), "el contador no se escribe con un valor calculado en Python"

    @pytest.mark.anyio
    async def test_increment_attempts_usa_inc(self):
        espia = _Espia(devuelve=MagicMock(attempts=3))
        repo = repos.BeanieVerificationRepository(document=espia)  # type: ignore[arg-type]

        assert await repo.increment_attempts(uuid4()) == 3
        assert espia.updates[0]["$inc"] == {"attempts": 1}

    @pytest.mark.anyio
    async def test_invalidate_for_filtra_los_pendientes(self):
        espia = _Espia(devuelve=MagicMock(modified_count=4))
        repo = repos.BeanieVerificationRepository(document=espia)  # type: ignore[arg-type]

        assert await repo.invalidate_for("ana@x.com", "magic_link", at=AHORA) == 4
        assert len(espia.filtros[0]) == 3, "identificador, propósito y `consumed_at is None`"

    @pytest.mark.anyio
    async def test_revoke_family_solo_toca_las_no_revocadas(self):
        espia = _Espia(devuelve=MagicMock(modified_count=2))
        repo = repos.BeanieSessionRepository(document=espia)  # type: ignore[arg-type]

        assert await repo.revoke_family(uuid4(), at=AHORA, reason="reuso") == 2
        assert len(espia.filtros[0]) == 2, "la familia y `revoked_at is None`"

    @pytest.mark.anyio
    async def test_un_update_de_usuario_inexistente_lanza(self):
        """
        Devolver silenciosamente haría que un caso de uso creyera que guardó. `KeyError` es lo
        mismo que hace el `FakeRepository` del kit.
        """
        from hexcore.darwin.testing import make_user

        espia = _Espia(devuelve=None)
        repo = repos.BeanieUserRepository(document=espia)  # type: ignore[arg-type]

        with pytest.raises(KeyError):
            await repo.update(make_user())


# ── Los mapeadores ────────────────────────────────────────────────────────────
class TestMapeo:
    def test_normaliza_los_datetime_naive(self):
        """
        ⚠️ Mongo devuelve los `datetime` **naive** (BSON UTC sin tzinfo), y compararlos con un
        `datetime.now(UTC)` levanta `TypeError`. Es el mismo problema que SQLite en el otro
        backend, y se resuelve igual: normalizando al hidratar.
        """
        from hexcore.darwin.infrastructure.orms.beanie.repositories import _aware

        naive = datetime(2026, 1, 1, 12, 0)

        normalizado = _aware(naive)

        assert normalizado is not None
        assert normalizado.tzinfo is UTC
        assert _aware(None) is None

    def test_no_toca_los_que_ya_son_aware(self):
        from hexcore.darwin.infrastructure.orms.beanie.repositories import _aware

        assert _aware(AHORA) == AHORA

    def test_la_entidad_sale_completa(self):
        """
        Con un objeto suelto y no con un `VerificationDocument`: instanciar un `Document` de Beanie
        sin `init_beanie` levanta `CollectionWasNotInitialized`, y este test es sobre el mapeador —
        que sólo lee atributos.
        """
        import types

        from hexcore.darwin.infrastructure.orms.beanie.repositories import _a_verificacion

        doc = types.SimpleNamespace(
            entity_id=uuid4(),
            identifier="ana@ejemplo.com",
            value_hash="h",
            purpose="magic_link",
            expires_at=AHORA + timedelta(hours=1),
            consumed_at=None,
            attempts=2,
            is_active=True,
            created_at=AHORA,
            updated_at=AHORA,
        )

        entidad = _a_verificacion(doc)

        assert isinstance(entidad, Verification)
        assert entidad.id == doc.entity_id
        assert entidad.purpose == "magic_link"
        assert entidad.attempts == 2
        assert entidad.expires_at.tzinfo is not None


# ── Con un Mongo real ─────────────────────────────────────────────────────────
@pytest.mark.mongo
@pytest.mark.skipif(not MONGO_URI, reason="sin HEXCORE_TEST_MONGO_URI")
class TestContraMongoReal:
    """
    El tier que prueba la **semántica**, no la forma de la consulta.

    Corre con `pytest -m mongo` y `HEXCORE_TEST_MONGO_URI` apuntando a un Mongo. Es donde se
    verifica lo único que un doble no puede: que de ocho canjes concurrentes gane exactamente uno.
    """

    @pytest.fixture(autouse=True)
    async def base(self):
        from pymongo import AsyncMongoClient

        from hexcore.darwin.infrastructure.orms.beanie.schema import (
            drop_identity_collections,
            init_identity_documents,
        )

        cliente = AsyncMongoClient(MONGO_URI)
        db = cliente.get_database("hexcore_test_darwin")
        await init_identity_documents(db)
        await drop_identity_collections(db)
        await init_identity_documents(db)
        yield db
        await drop_identity_collections(db)
        await cliente.close()

    @pytest.mark.anyio
    async def test_el_alta_y_la_lectura(self):
        from hexcore.darwin.testing import make_user

        repo = repos.BeanieUserRepository()
        ana = make_user("ana@ejemplo.com")

        await repo.add(ana)

        assert (await repo.get_by_email("ana@ejemplo.com")).id == ana.id
        assert (await repo.get_by_id(ana.id)).email == "ana@ejemplo.com"

    @pytest.mark.anyio
    async def test_bump_concurrente_no_pierde_incrementos(self):
        """La prueba de que `$inc` hace lo que el docstring dice."""
        import asyncio

        from hexcore.darwin.testing import make_user

        repo = repos.BeanieUserRepository()
        ana = await repo.add(make_user("ana@ejemplo.com"))

        resultados = await asyncio.gather(
            *(repo.bump_token_generation(ana.id) for _ in range(10))
        )

        assert sorted(resultados) == list(range(1, 11))
        assert (await repo.get_by_id(ana.id)).token_generation == 10

    @pytest.mark.anyio
    async def test_ocho_canjes_concurrentes_dejan_pasar_uno(self):
        """
        ⚠️ **Lo único que ningún doble puede probar.** Es la propiedad de la que depende la
        detección de reuso.
        """
        import asyncio

        repo = repos.BeanieVerificationRepository()
        await repo.add(
            Verification(
                identifier="ana@ejemplo.com",
                value_hash="h",
                purpose="magic_link",
                expires_at=AHORA + timedelta(hours=1),
            )
        )

        resultados = await asyncio.gather(
            *(
                repo.consume("ana@ejemplo.com", "magic_link", "h", at=AHORA)
                for _ in range(8)
            )
        )

        assert sum(1 for r in resultados if r is not None) == 1

    @pytest.mark.anyio
    async def test_dos_rotaciones_concurrentes_dejan_pasar_una(self):
        import asyncio

        from hexcore.darwin.domain.entities import IdentitySession

        repo = repos.BeanieSessionRepository()
        sesion = await repo.add(
            IdentitySession(
                actor_user_id=uuid4(),
                subject_user_id=uuid4(),
                token_hash="h",
                expires_at=AHORA + timedelta(days=1),
            )
        )

        resultados = await asyncio.gather(
            *(repo.consume_for_rotation(sesion.id, at=AHORA) for _ in range(8))
        )

        assert sum(1 for r in resultados if r is not None) == 1

    @pytest.mark.anyio
    async def test_el_mail_repetido_lo_rechaza_el_indice(self):
        from pymongo.errors import DuplicateKeyError

        from hexcore.darwin.testing import make_user

        repo = repos.BeanieUserRepository()
        await repo.add(make_user("ana@ejemplo.com"))

        with pytest.raises(DuplicateKeyError):
            await repo.add(make_user("ana@ejemplo.com"))
