"""
Inicialización de los documentos de identidad en Beanie. Requiere el extra `[darwin-beanie]`.

⚠️ **Esto no es el equivalente cosmético de `create_identity_tables`: es obligatorio.** Un `Document`
de Beanie no funciona hasta que `init_beanie` lo ve — sin eso, la primera consulta falla con
`CollectionWasNotInitialized`. En SQL, olvidarse de importar los modelos produce un problema de
migración; acá produce una app que no arranca.

Y hay un segundo motivo por el que hace falta una función propia: `discover_beanie_documents()` del
framework recorre las subclases de `BaseDocument`, y los documentos de Darwin **no heredan de ahí**
—ver el docstring de `documents.py`, donde está el motivo de seguridad— así que el descubrimiento
automático no los encuentra.

Uso, en el arranque::

    from hexcore.darwin.infrastructure.orms.beanie.schema import init_identity_documents

    await init_identity_documents()

O sumándolos a los propios en una sola llamada a `init_beanie`::

    from hexcore.darwin.infrastructure.orms.beanie.schema import identity_documents

    await init_beanie(database=db, document_models=[*mis_documentos, *identity_documents()])
"""
from __future__ import annotations

import typing as t

__all__ = [
    "identity_documents",
    "init_identity_documents",
    "drop_identity_collections",
]


def identity_documents(
    documents: t.Sequence[type] | None = None,
) -> list[type]:
    """
    Los documentos de identidad, para pasárselos a `init_beanie`.

    Args:
        documents: Los que quieras en vez de los seis por defecto. Pasá los tuyos si subclaseaste
            alguno — sobre todo el de usuario, que es el que se extiende.

    Uso::

        from hexcore.darwin.infrastructure.orms.beanie.schema import identity_documents

        assert len(identity_documents()) == 6
    """
    from hexcore.darwin.infrastructure.orms.beanie.documents import IDENTITY_DOCUMENTS

    return list(documents if documents is not None else IDENTITY_DOCUMENTS)


async def init_identity_documents(
    database: t.Any = None,
    *,
    documents: t.Sequence[type] | None = None,
    client: t.Any = None,
) -> None:
    """
    Inicializa los documentos de identidad contra la base.

    Args:
        database: La base. Si no viene, se abre un cliente con `ServerConfig.mongo_uri` y se usa su
            base por defecto — igual que `init_beanie_documents()` del framework.
        documents: Los documentos. Por defecto, los seis.
        client: Un `AsyncMongoClient` propio, para reusar un pool.

    ⚠️ **Llamar a `init_beanie` dos veces sobre la misma base es válido pero no acumulativo**: la
    segunda llamada reemplaza el registro de la primera. Si tenés documentos propios, inicializá
    todo junto con `identity_documents()` en vez de llamar a esta función aparte.

    Uso::

        await init_identity_documents()
    """
    # `pyright: ignore` narrow y con motivo, que es la política de la casa: ni `beanie` ni
    # `pymongo` shippean stubs completos, así que sus símbolos llegan parcialmente desconocidos.
    # Se silencia la regla exacta y en la línea exacta — un `# type: ignore` pelado, que es lo que
    # usa la capa Beanie preexistente, taparía también un error real de tipos en la llamada.
    from beanie import init_beanie  # pyright: ignore[reportUnknownVariableType]

    objetivo = identity_documents(documents)

    if database is None:
        from pymongo import AsyncMongoClient

        from hexcore.config import LazyConfig

        motor = client or AsyncMongoClient(  # pyright: ignore[reportUnknownVariableType]
            LazyConfig.get_config().mongo_uri
        )
        # `database` ya está declarado `t.Any` en la firma, pero pyright lo re-estrecha con
        # lo que devuelve `motor`, que es desconocido. La reasignación explícita lo contiene.
        database = t.cast(t.Any, motor.get_default_database())

    await init_beanie(database=database, document_models=objetivo)


async def drop_identity_collections(
    database: t.Any = None, *, documents: t.Sequence[type] | None = None
) -> None:
    """
    Borra las colecciones de identidad. **Sólo para tests.**

    No hay orden que respetar —Mongo no valida referencias— así que a diferencia de
    `drop_identity_tables` esto no tiene que ir en reversa. Es una de las pocas cosas que el
    backend de Mongo simplifica de verdad.
    """
    objetivo = identity_documents(documents)

    for documento in objetivo:
        coleccion = getattr(documento, "get_pymongo_collection", None)
        if coleccion is None:  # pragma: no cover - documento sin inicializar
            continue
        await coleccion().drop()
