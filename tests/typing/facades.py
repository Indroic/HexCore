"""
Las tres fachadas canónicas NO deben tipar `Any`.

Es la demostración ejecutable de que el problema #1 de DX está muerto. Medido antes de los
stubs, con Pyright sobre este mismo archivo:

    El tipo de "Base" es "Any"
    El tipo de "SqlAlchemyUnitOfWork" es "Any"
    El tipo de "Command" es "Any"
    El tipo de "create_app" es "Any"

`hexcore/cqrs.py`, `hexcore/sql.py` y `hexcore/fastapi.py` resuelven sus exports con
`_EXPORTS` + `__getattr__` y declaran `__all__ = sorted(_EXPORTS)`. Las dos son expresiones
de runtime, así que sin los `.pyi` generados los 126 símbolos de la superficie pública
recomendada tipan `Any`.

Este archivo no se ejecuta: lo chequea Pyright vía `tests/test_typing_gate.py`.
"""
from __future__ import annotations

from typing import assert_type

# ── hexcore.sql ───────────────────────────────────────────────────────────────
from hexcore.sql import (
    NAMING_CONVENTION,
    Base,
    PoolSettings,
    QueryRequestDTO,
    SqlAlchemyUnitOfWork,
    init_engine,
)

# ── hexcore.cqrs ──────────────────────────────────────────────────────────────
from hexcore.cqrs import (
    Command,
    HandlerRegistry,
    InMemoryCommandBus,
    MiddlewarePipeline,
)

# ── hexcore.fastapi ───────────────────────────────────────────────────────────
from hexcore.fastapi import AppFeatures, create_app, rate_limit


def las_clases_son_clases_y_no_any() -> None:
    """`assert_type` falla si el tipo es `Any`, que es exactamente lo que se quiere fijar."""
    assert_type(Base, type[Base])
    assert_type(SqlAlchemyUnitOfWork, type[SqlAlchemyUnitOfWork])
    assert_type(Command, type[Command])
    assert_type(HandlerRegistry, type[HandlerRegistry])
    assert_type(InMemoryCommandBus, type[InMemoryCommandBus])
    assert_type(MiddlewarePipeline, type[MiddlewarePipeline])
    assert_type(AppFeatures, type[AppFeatures])
    assert_type(PoolSettings, type[PoolSettings])
    assert_type(QueryRequestDTO, type[QueryRequestDTO])


def las_constantes_conservan_su_tipo() -> None:
    assert_type(NAMING_CONVENTION, dict[str, str])


def las_firmas_se_chequean_de_verdad() -> None:
    """
    Lo que de verdad importa: con `Any` cualquier llamada pasaba, incluso una mal escrita.

    Con los stubs, Pyright ve la firma real, así que un kwarg inexistente o un tipo
    equivocado se detecta en el editor del consumidor.
    """
    app = create_app()
    reveal_type(app)  # noqa: F821  -> FastAPI

    registry = HandlerRegistry(allow_override=True)
    reveal_type(registry.registered_commands)  # noqa: F821  -> frozenset[type[Command]]

    pipeline = MiddlewarePipeline()
    reveal_type(pipeline.add)  # noqa: F821

    limiter = rate_limit(5, 300)
    reveal_type(limiter)  # noqa: F821

    reveal_type(init_engine)  # noqa: F821
    reveal_type(PoolSettings(pre_ping=False))  # noqa: F821


# ── hexcore.darwin ────────────────────────────────────────────────────────────
from hexcore.darwin import (  # noqa: E402
    AuthContext,
    Impersonation,
    Principal,
    RoleRegistry,
    UnauthenticatedError,
    require_auth,
)


def la_fachada_de_darwin_no_tipa_any() -> None:
    assert_type(Principal, type[Principal])
    assert_type(Impersonation, type[Impersonation])
    assert_type(RoleRegistry, type[RoleRegistry])
    assert_type(UnauthenticatedError, type[UnauthenticatedError])


def el_authcontext_es_generico_y_se_ve_en_el_ide() -> None:
    """
    El parámetro genérico es lo que hace que la personalización sea visible.

    Sin él, `require_auth().user` tiparía `Any` y la promesa de "tu handler recibe el usuario
    extendido" no sería verificable por el checker.
    """
    class MiUsuario:
        plan: str

    ctx: AuthContext[MiUsuario] = AuthContext(
        actor=Principal(user_id=__import__("uuid").uuid4()),
        subject=Principal(user_id=__import__("uuid").uuid4()),
        transport="cookie",
    )
    reveal_type(ctx.user)  # noqa: F821  -> MiUsuario | None

    generico = require_auth()
    reveal_type(generico.actor)  # noqa: F821  -> Principal | SystemPrincipal
    reveal_type(generico.is_impersonating)  # noqa: F821  -> bool
