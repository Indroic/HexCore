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


def build_root_router(
    prefix: str,
    children: t.Mapping[str, APIRouter],
    *,
    dependencies: t.Sequence[t.Any] = (),
    tags: list[str] | None = None,
    **router_kwargs: t.Any,
) -> APIRouter:
    """
    Construye un router raíz con sus hijos ya incluidos.

    Args:
        prefix: Prefijo del router raíz (p. ej. ``"/admin"``).
        children: Mapa ``{sub_prefijo: router}``. Un sub-prefijo vacío monta el hijo
            directamente sobre el prefijo raíz.
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
    """
    root = APIRouter(
        prefix=prefix,
        dependencies=list(dependencies),
        tags=t.cast(t.Any, tags),
        **router_kwargs,
    )
    for child_prefix, child in children.items():
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
