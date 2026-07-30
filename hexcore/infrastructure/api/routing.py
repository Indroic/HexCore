"""
Composición declarativa de routers.

Reemplaza los ficheros `*_root_router.py` que toda app escribe por triplicado: crear un
`APIRouter` con prefijo, tags y `dependencies=[Depends(auth)]`, e incluir N routers hijos
con su sub-prefijo. Eso se puede declarar en vez de escribirse.
"""
from __future__ import annotations

import typing as t

from fastapi import APIRouter, FastAPI

__all__ = ["build_root_router", "mount_routers"]

# Un router a montar: el router solo, o el router con kwargs para `include_router`
# (`prefix`, `tags`, `dependencies`…).
MountableRouter = t.Union[APIRouter, t.Tuple[APIRouter, t.Dict[str, t.Any]]]

# Los hijos de un router raíz. El `Mapping` es la forma bonita cuando cada hijo tiene su
# sub-prefijo; la secuencia existe porque un dict **no puede** tener dos claves `""`, y un
# raíz cuyos hijos ya declaran sus rutas completas necesita exactamente eso.
RouterChildren = t.Union[
    t.Mapping[str, APIRouter],
    t.Sequence[t.Union[APIRouter, t.Tuple[str, APIRouter]]],
]


def _iter_children(
    children: RouterChildren,
) -> t.Iterator[t.Tuple[str, APIRouter]]:
    """Normaliza las dos formas de `children` a pares ``(sub_prefijo, router)``."""
    if isinstance(children, t.Mapping):
        yield from children.items()
        return

    for entry in children:
        if isinstance(entry, tuple):
            yield entry
        else:
            # Un router pelado en la secuencia = sin sub-prefijo. Es el caso que motivó
            # aceptar secuencias, así que merece no tener que escribir `("", router)`.
            yield "", entry


def build_root_router(
    prefix: str,
    children: RouterChildren,
    *,
    dependencies: t.Sequence[t.Any] = (),
    tags: list[str] | None = None,
    **router_kwargs: t.Any,
) -> APIRouter:
    """
    Construye un router raíz con sus hijos ya incluidos.

    Args:
        prefix: Prefijo del router raíz (p. ej. ``"/admin"``).
        children: Los hijos, en cualquiera de las dos formas:

            - **Mapa** ``{sub_prefijo: router}``. Un sub-prefijo vacío monta el hijo
              directamente sobre el prefijo raíz.
            - **Secuencia** de routers o de pares ``(sub_prefijo, router)``. Un router
              pelado equivale a sub-prefijo vacío.

            La secuencia no es azúcar: un dict no puede tener **dos** claves ``""``, así
            que un raíz con varios hijos que ya traen su propio prefijo —lo normal cuando
            cada feature declara sus rutas completas— no se puede expresar con un mapa.
        dependencies: Dependencias comunes a todos los hijos (típicamente la auth).
        tags: Tags comunes.
        **router_kwargs: Se pasan tal cual a `APIRouter` (``responses``,
            ``deprecated``…).

    Returns:
        El `APIRouter` raíz, listo para `app.include_router`.

    Uso::

        admin_router = build_root_router(
            "/admin",
            {"/users": users_router, "/reports": reports_router},
            dependencies=[Depends(require_admin)],
            tags=["admin"],
        )

        # Hijos que ya declaran sus rutas completas: no hay sub-prefijo que poner.
        api_router = build_root_router("/api/v1", [usuarios_router, tickets_router])
    """
    root = APIRouter(
        prefix=prefix,
        dependencies=list(dependencies),
        tags=t.cast(t.Any, tags),
        **router_kwargs,
    )
    for child_prefix, child in _iter_children(children):
        root.include_router(child, prefix=child_prefix)
    return root


def mount_routers(
    app: FastAPI,
    routers: t.Sequence[MountableRouter],
) -> None:
    """
    Monta una lista de routers en la app.

    Sustituye las tiras de `include_router` consecutivas de `main.py`. Cada elemento
    puede ser un `APIRouter` o una tupla ``(router, kwargs)`` con los argumentos extra
    de `include_router`.

    Uso::

        mount_routers(app, [
            usuarios_router,
            (tickets_router, {"prefix": "/v2", "tags": ["tickets-v2"]}),
        ])
    """
    for entry in routers:
        if isinstance(entry, tuple):
            router, kwargs = entry
            app.include_router(router, **kwargs)
        else:
            app.include_router(entry)
