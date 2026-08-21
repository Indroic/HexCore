"""
Sub-app de Typer para Darwin: `hexcore identity ...`.

⚠️ **Este módulo sólo puede importar `typer` y stdlib en el nivel superior.**
`hexcore/__init__.py` importa `hexcore.infrastructure.cli` **eagerly**, y `cli.py` hace
`add_typer(identity_cli)`, así que todo lo que se importe acá arriba se carga con
`import hexcore` — en cualquier proceso, tenga o no los extras. Un `from hexcore.darwin
import ...` en el nivel superior arrastraría sqlalchemy y rompería el contrato que
`tests/test_optional_dependencies.py` verifica.

Por eso cada comando importa lo que necesita **dentro de su cuerpo**. Es el mismo estilo que
`make_migrations` y `test` ya usan con `subprocess`.
"""
from __future__ import annotations

import typing as t

import typer

__all__ = ["identity_cli"]

identity_cli = typer.Typer(
    help="Comandos de Darwin, el módulo de identidad: claves, esquema y diagnóstico."
)


@identity_cli.command(name="generate-secret")
def generate_secret() -> None:
    """
    Genera una clave de firma para `HEXCORE_DARWIN_SECRET_KEY`.

    Existe porque el secreto **no tiene default** a propósito, y el primer obstáculo de quien
    cablea Darwin es tener que buscar cómo generar uno. Un comando lo resuelve sin que nadie
    caiga en la tentación de poner `"changeme"`.
    """
    import secrets

    typer.echo(secrets.token_urlsafe(48))


@identity_cli.command(name="generate-keys")
def generate_keys(
    algorithm: str = typer.Option(
        "Ed25519", "--algorithm", "-a", help="Algoritmo de firma."
    ),
    kid: str = typer.Option(None, "--kid", help="Identificador de la clave."),
) -> None:
    """
    Genera un par de claves de firma de tokens, en JWK.

    La salida va a stdout para que se pueda redirigir a un secret manager. **La privada sale
    en claro**: no la dejes en el historial del shell ni en un archivo del repo. El aviso va
    a stderr justamente para que no ensucie lo que se redirige.

    Los dos JWK se emiten **parseados y no como el string que guarda `SigningKey`**: un
    secret manager que recibe un JSON con un string de JSON adentro obliga a un doble parseo
    del otro lado, y ese es el paso que alguien resuelve pegando la privada en un archivo
    intermedio.
    """
    import json

    from hexcore.darwin.infrastructure.keys import generate_signing_key

    clave = generate_signing_key(algorithm=algorithm, kid=kid)
    typer.secho(
        "La clave privada sale en claro por stdout. Redirigila a tu secret manager.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    typer.echo(
        json.dumps(
            {
                "kid": clave.kid,
                "algorithm": clave.algorithm,
                "status": clave.status,
                "public_jwk": json.loads(clave.public_key),
                "private_jwk": json.loads(clave.private_key),
            },
            indent=2,
        )
    )


@identity_cli.command(name="create-tables")
def create_tables() -> None:
    """
    Crea las tablas de identidad. **Atajo para desarrollo y tests.**

    En producción usá Alembic: `create_identity_tables` es idempotente pero no versiona nada,
    así que un cambio de esquema más adelante no tiene desde dónde migrar.
    """
    import asyncio

    from hexcore.darwin.infrastructure.schema import create_identity_tables
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import init_engine
    from hexcore.config import LazyConfig

    init_engine(LazyConfig.get_config().async_sql_database_url)
    asyncio.run(create_identity_tables())
    typer.secho("Tablas de identidad creadas.", fg=typer.colors.BRIGHT_GREEN)


@identity_cli.command(name="check-schema")
def check_schema() -> None:
    """
    Verifica que las tablas de identidad estén en `Base.metadata`.

    Es el chequeo que evita la pérdida de datos más cara del módulo: una tabla que existe en la
    base y falta en el metadata hace que `alembic revision --autogenerate` emita
    `op.drop_table` sobre ella. Con Darwin, eso es el almacén de credenciales completo.

    Sale con código 1 si falta alguna, para poder ponerlo en un pre-commit o en CI.
    """
    from hexcore.darwin.infrastructure.models import IDENTITY_MODELS
    from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

    registradas = set(Base.metadata.tables)
    faltan = sorted(
        m.__tablename__ for m in IDENTITY_MODELS if m.__tablename__ not in registradas
    )

    if not faltan:
        typer.secho(
            f"Las {len(IDENTITY_MODELS)} tablas de identidad están en Base.metadata.",
            fg=typer.colors.BRIGHT_GREEN,
        )
        return

    typer.secho(
        f"Faltan en Base.metadata: {', '.join(faltan)}.\n\n"
        f"`alembic revision --autogenerate` les va a emitir op.drop_table. Importalas desde "
        f"tu paquete models/, o agregá `ensure_identity_schema_loaded()` al env.py.",
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


@identity_cli.command(name="plugins")
def list_plugins(
    module: str = typer.Argument(
        ...,
        help="Módulo con un `plugins: PluginRegistry` o `PLUGINS: list[DarwinPlugin]`.",
    ),
) -> None:
    """
    Lista los plugins de un módulo, **en su orden de ejecución resuelto**.

    Es el comando de diagnóstico del sistema de plugins: el orden es topológico por `requires`
    y no el de registro, así que verlo impreso es la única forma de confirmar que un plugin
    corre donde uno cree.
    """
    import importlib

    from hexcore.darwin.application.plugins import PluginRegistry

    modulo = importlib.import_module(module)

    # Los `getattr` sobre un módulo importado en runtime devuelven `Any`: se anota el destino
    # para que el resto de la función quede tipada, en vez de propagar el desconocido.
    registro: PluginRegistry | None = getattr(modulo, "plugins", None)
    if registro is None:
        declarados: list[t.Any] | None = getattr(modulo, "PLUGINS", None)
        if declarados is None:
            typer.secho(
                f"'{module}' no expone `plugins` ni `PLUGINS`.", fg=typer.colors.RED
            )
            raise typer.Exit(code=1)
        registro = PluginRegistry(declarados)

    registro.validate()
    for i, nombre in enumerate(registro.names, start=1):
        plugin = registro.get(nombre)
        requiere = ", ".join(type(plugin).requires) if plugin else ""
        sufijo = f"  (requiere: {requiere})" if requiere else ""
        typer.echo(f"{i}. {nombre}{sufijo}")
