"""
Sub-app de Typer del plugin `rbac`: sincronización, listado y asignación de roles.

⚠️ **No lo monta `hexcore identity ...`.** El núcleo nunca importa un plugin por nombre — es
la misma regla que separa `[darwin]` en tres piezas instalables, y `test_darwin_plugin_decoupling.py`
la verifica recorriendo los imports de `hexcore/darwin/{domain,application,infrastructure}`. Un
plugin puede depender del núcleo; el núcleo no puede depender de ningún plugin, ni siquiera
para ofrecerle un lugar en su propio árbol de comandos.

Por eso este `rbac_cli` es un `typer.Typer` **suelto**, que el consumidor monta en su propia
app::

    # myapp/cli.py
    from hexcore.infrastructure.cli import app
    from hexcore.darwin.plugins.rbac.cli import rbac_cli

    app.add_typer(rbac_cli, name="rbac")

Uso::

    python -m myapp.cli rbac sync myapp.bootstrap
    python -m myapp.cli rbac list myapp.bootstrap --scope org:1
    python -m myapp.cli rbac assign myapp.bootstrap --user-id ... --role admin
"""
from __future__ import annotations

import typing as t

import typer

__all__ = ["rbac_cli"]

rbac_cli = typer.Typer(
    help="Roles y permisos persistidos del plugin `rbac`: sincronización, listado y asignación."
)

#: Los tres comandos toman el mismo argumento: un módulo que, **al importarse, deja Darwin
#: configurado** con el plugin `rbac` registrado — el mismo `configure_identity(...)` que la
#: app real llama al arrancar. No hay forma de que este CLI adivine esa configuración.
_MODULE_HELP = (
    "Módulo que, al importarse, deja Darwin configurado (llama a `configure_identity(...)`) "
    "con el plugin `rbac` registrado."
)


def _plugin_rbac(module: str) -> t.Any:
    import importlib

    from hexcore.darwin.application.container import get_identity_container
    from hexcore.darwin.plugins.rbac import RbacPlugin

    importlib.import_module(module)
    plugin = get_identity_container().plugins.get(RbacPlugin.name)
    if plugin is None:
        typer.secho(
            f"'{module}' no registró el plugin 'rbac'. Revisá que su "
            f"`configure_identity(plugins=...)` lo incluya.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    return plugin


@rbac_cli.command(name="sync")
def rbac_sync(module: str = typer.Argument(..., help=_MODULE_HELP)) -> None:
    """
    Sincroniza el `RoleRegistry` de código a la tabla.

    Es lo que `RbacSeedStep` hace al arrancar la app; este comando sirve para correrlo a mano
    —en un deploy sin lifespan, o para confirmar que un rol nuevo del código ya llegó a la
    base— sin tener que levantar el servidor entero.
    """
    import asyncio

    plugin = _plugin_rbac(module)
    asyncio.run(plugin.service().sync_system_roles(plugin.registry()))
    typer.secho("Roles de código sincronizados.", fg=typer.colors.BRIGHT_GREEN)


@rbac_cli.command(name="list")
def rbac_list(
    module: str = typer.Argument(..., help=_MODULE_HELP),
    scope: str = typer.Option("", "--scope", help="Scope a listar. Vacío = global."),
) -> None:
    """Lista los roles de un scope, con su origen (código o API)."""
    import asyncio

    plugin = _plugin_rbac(module)
    roles = asyncio.run(plugin.service().list_roles(scope))
    if not roles:
        typer.echo(f"Sin roles en el scope '{scope or '(global)'}'.")
        return
    for rol in roles:
        origen = "sistema" if rol.is_system else "api"
        typer.echo(f"{rol.name}  [{origen}]  {rol.description}".rstrip())


@rbac_cli.command(name="assign")
def rbac_assign(
    module: str = typer.Argument(..., help=_MODULE_HELP),
    actor_id: str | None = typer.Option(
        None,
        "--actor-id",
        help=(
            "Quién otorga, para la anti-escalada: sólo se asigna un rol cuyos permisos ese "
            "usuario ya tiene efectivamente en el scope. Sin `--actor-id`, la asignación es "
            "del sistema y saltea la anti-escalada — es el único bootstrap posible cuando "
            "todavía nadie tiene ningún permiso, y queda auditado como tal en la fila "
            "(`granted_by=NULL`)."
        ),
    ),
    user_id: str = typer.Option(..., "--user-id", help="A quién se le asigna."),
    role_name: str = typer.Option(..., "--role", help="Nombre del rol, en `--scope`."),
    scope: str = typer.Option("", "--scope", help="Scope de la asignación. Vacío = global."),
) -> None:
    """
    Asigna un rol a un usuario.

    **Misma anti-escalada que la API** cuando se pasa `--actor-id`; sin él, es un
    otorgamiento del sistema — ver el docstring de `RbacService.assign_role`.
    """
    import asyncio
    from uuid import UUID

    from hexcore.darwin.plugins.rbac.domain import RoleNotFoundError

    plugin = _plugin_rbac(module)
    servicio = plugin.service()

    async def _asignar() -> None:
        encontrado = await servicio.list_roles(scope)
        coincidencia = next((r for r in encontrado if r.name == role_name), None)
        if coincidencia is None:
            raise RoleNotFoundError(
                f"No existe el rol '{role_name}' en el scope '{scope or '(global)'}'."
            )
        await servicio.assign_role(
            actor_id=UUID(actor_id) if actor_id is not None else None,
            user_id=UUID(user_id),
            role_id=coincidencia.id,
            scope_key=scope,
        )

    try:
        asyncio.run(_asignar())
    except RoleNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"'{role_name}' asignado a {user_id} en el scope '{scope or '(global)'}'.",
        fg=typer.colors.BRIGHT_GREEN,
    )
