from __future__ import annotations
import os
from pathlib import Path
import typer
from async_typer import AsyncTyper  # type: ignore
from hexcore.config import LazyConfig

app = AsyncTyper(
    help="CLI para ayudar con tareas de desarrollo en el proyecto Euphoria."
)

# Asumiendo que el script se ejecuta desde la raíz del proyecto.
PROJECT_ROOT = LazyConfig.get_config().base_dir
DOMAIN_PATH = PROJECT_ROOT / "src" / "domain"
APPLICATION_PATH = PROJECT_ROOT / "src" / "application"
INFRAESTRUCTURE_PATH = PROJECT_ROOT / "src" / "infrastructure"
DB_PATH = INFRAESTRUCTURE_PATH / "database"

MODELS_PATH = DB_PATH / "models"
DOCUMENTS_PATH = DB_PATH / "documents"

TESTS_DOMAIN_PATH = PROJECT_ROOT / "tests" / "domain"

README = PROJECT_ROOT / "README.md"
GITIGNORE = PROJECT_ROOT / ".gitignore"
MANAGE = PROJECT_ROOT / "manage.py"


@app.command(name="init")
def init_project(
    project_name: str = typer.Argument(..., help="Nombre del proyecto a crear"),
    template: str = typer.Option(
        "hexagonal",
        "--template",
        "-t",
        help="Template de estructura: 'hexagonal' o 'vertical-slice'.",
    ),
) -> None:
    """
    Inicializa un proyecto base usando un template de estructura.
    """
    normalized_template = template.strip().lower()
    if normalized_template not in {"hexagonal", "vertical-slice"}:
        typer.secho(
            "Error: template invalido. Usa 'hexagonal' o 'vertical-slice'.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    base_path = Path.cwd() / project_name
    if base_path.exists():
        typer.secho(f"Error: La carpeta '{base_path}' ya existe.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    base_path.mkdir(parents=True, exist_ok=False)
    typer.echo(
        f"Inicializando el proyecto en: {base_path} (template: {normalized_template})"
    )

    if normalized_template == "hexagonal":
        directories = [
            "src/domain",
            "src/application",
            "src/infrastructure",
            "src/infrastructure/database",
            "src/infrastructure/database/models",
            "src/infrastructure/database/documents",
            "tests/domain",
        ]
        repository_paths = {
            "src.infrastructure.repositories",
            "src.infrastructure.repositories.implementations",
        }
        models_import = "import src.infrastructure.database.models as models"
        migrations_root = "src/infrastructure/database/migrations"
    else:
        directories = [
            "src/features",
            "src/shared",
            "src/shared/domain",
            "src/shared/application",
            "src/shared/infrastructure",
            "src/shared/infrastructure/database",
            "src/shared/infrastructure/database/models",
            "src/shared/infrastructure/database/documents",
            "tests/features",
        ]
        repository_paths = {
            "src.features",
            "src.shared.infrastructure.repositories",
        }
        models_import = "import src.shared.infrastructure.database.models as models"
        migrations_root = "src/shared/infrastructure/database/migrations"

    for relative_dir in directories:
        _create_package_directory(base_path, relative_dir)

    _write_project_base_files(base_path, project_name)
    _write_root_config(base_path, sorted(repository_paths))
    _setup_alembic(
        base_path, models_import=models_import, migrations_root=migrations_root
    )

    import subprocess

    subprocess.run(["ruff", "format"], cwd=base_path)

    typer.secho(
        f"\n¡Proyecto '{project_name}' inicializado exitosamente en {base_path} con template '{normalized_template}'!",
        fg=typer.colors.BRIGHT_GREEN,
    )


@app.command(name="create-domain-module")
def create_domain_module(
    name: str = typer.Argument(
        ..., help="El nombre del nuevo módulo de dominio (ej. 'ventas', 'marketing')."
    ),
) -> None:
    """
    Crea un nuevo módulo de dominio con una estructura de archivos básica.
    """
    module_name = name.lower().strip()
    if not module_name.isidentifier():
        typer.secho(
            f"Error: El nombre '{name}' no es un identificador válido de Python.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    module_path = DOMAIN_PATH / module_name

    if module_path.exists():
        typer.secho(
            f"Error: El módulo de dominio '{module_name}' ya existe en {module_path}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Creando el módulo de dominio '{module_name}' en: {module_path}")

    try:
        # 1. Crear directorio del módulo de dominio
        module_path.mkdir(parents=True)
        typer.secho(
            f"Directorio de código creado: {module_path}", fg=typer.colors.GREEN
        )

        # 2. Crear archivos base de dominio
        class_name = module_name.capitalize()
        files_to_create = {
            "__init__.py": "",
            "repositories.py": _get_repositories_template(class_name),
            "services.py": _get_services_template(module_name, class_name),
            "value_objects.py": _get_value_objects_template(),
            "events.py": _get_events_template(),
            "enums.py": "",
            "exceptions.py": "",
        }

        for filename, content in files_to_create.items():
            file_path = module_path / filename
            file_path.write_text(content.strip(), encoding="utf-8")
            typer.secho(f"  -> Archivo creado: {file_path}", fg=typer.colors.GREEN)

        # 3. Crear directorio y archivos de test
        test_module_path = TESTS_DOMAIN_PATH / module_name
        typer.echo(f"Creando el módulo de tests '{module_name}' en: {test_module_path}")
        test_module_path.mkdir(parents=True, exist_ok=True)
        typer.secho(
            f"Directorio de tests creado: {test_module_path}", fg=typer.colors.GREEN
        )

        # Crear __init__.py para que sea un paquete de tests
        (test_module_path / "__init__.py").touch()
        typer.secho(
            f"  -> Archivo creado: {test_module_path / '__init__.py'}",
            fg=typer.colors.GREEN,
        )

        # Crear archivo de test para entidades
        test_entities_path = test_module_path / f"test_{module_name}_entities.py"
        typer.secho(
            f"  -> Archivo de test creado: {test_entities_path}", fg=typer.colors.GREEN
        )

        typer.secho(
            f"\n¡Módulo de dominio '{module_name}' creado exitosamente!",
            fg=typer.colors.BRIGHT_GREEN,
        )
        typer.echo("Siguientes pasos sugeridos:")
        typer.echo(
            f"1. Define tus entidades principales en 'src/domain/{module_name}/entities.py'."
        )
        typer.echo(
            f"2. Define las interfaces de repositorio en 'src/domain/{module_name}/repositories.py'."
        )
        typer.echo(
            f"3. Escribe pruebas para tus entidades en '{test_entities_path.relative_to(PROJECT_ROOT)}'."
        )

    except OSError as e:
        typer.secho(f"Error al crear el módulo: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command(name="make-migrations")
def make_migrations(
    description: str = typer.Argument(..., help="Descripción de la migración"),
) -> None:
    command = f"alembic revision --autogenerate -m '{description}'"
    typer.echo(f"Ejecutando comando de migración: {command}")
    os.system(command)


@app.command(name="migrate")
def migrate() -> None:
    """
    Ejecuta las migraciones pendientes en la base de datos.
    """
    command = "alembic upgrade head"
    typer.echo(f"Ejecutando comando de migración: {command}")
    os.system(command)


@app.command(name="test")
def test(
    path: str = typer.Argument(
        "test", help="Ruta a los tests a ejecutar (por defecto: test/)"
    ),
    extra_args: str = typer.Option(
        "", help="Argumentos extra para pytest, ej: -k 'nombre'"
    ),
) -> None:
    """
    Ejecuta los tests del proyecto usando pytest.
    """
    import subprocess
    import shlex

    typer.echo(f"Ejecutando tests en: {path}")
    cmd = f"pytest {shlex.quote(path)} {extra_args}".strip()
    raise SystemExit(subprocess.call(cmd, shell=True))


def _create_package_directory(base_path: Path, relative_dir: str) -> None:
    target_path = base_path / relative_dir
    target_path.mkdir(parents=True, exist_ok=True)
    current = target_path
    while current != base_path:
        init_file = current / "__init__.py"
        if not init_file.exists():
            init_file.touch()
        current = current.parent
    typer.secho(f"Directorio creado: {target_path}", fg=typer.colors.GREEN)


def _write_project_base_files(base_path: Path, project_name: str) -> None:
    readme = base_path / "README.md"
    gitignore = base_path / ".gitignore"
    manage = base_path / "manage.py"

    if not readme.exists():
        readme.write_text(
            f"# Proyecto {project_name}\n\nDescripción del proyecto.", encoding="utf-8"
        )
        typer.secho(f"Archivo creado: {readme}", fg=typer.colors.GREEN)

    if not gitignore.exists():
        gitignore.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
        typer.secho(f"Archivo creado: {gitignore}", fg=typer.colors.GREEN)

    if not manage.exists():
        manage.write_text(_get_manage_template().strip(), encoding="utf-8")
        typer.secho(f"Archivo creado: {manage}", fg=typer.colors.GREEN)


def _write_root_config(base_path: Path, repository_paths: list[str]) -> None:
    config_file = base_path / "config.py"
    if config_file.exists():
        typer.secho(
            f"Archivo existente preservado: {config_file}", fg=typer.colors.YELLOW
        )
        return

    repository_paths_text = "\n".join(f'        "{path}",' for path in repository_paths)
    config_file.write_text(
        f"""from hexcore.config import ServerConfig


config = ServerConfig(
    repository_discovery_paths={{
{repository_paths_text}
    }}
)
""",
        encoding="utf-8",
    )
    typer.secho(f"Archivo creado: {config_file}", fg=typer.colors.GREEN)


def _setup_alembic(base_path: Path, models_import: str, migrations_root: str) -> None:
    import subprocess

    subprocess.run(["alembic", "init", "alembic"], cwd=base_path)
    typer.secho(f"Directorio creado: {base_path / 'alembic'}", fg=typer.colors.GREEN)

    migrations_versions = f"{migrations_root}/versions"
    (base_path / migrations_versions).mkdir(parents=True, exist_ok=True)

    alembic_ini_path = base_path / "alembic.ini"
    if alembic_ini_path.exists():
        content = alembic_ini_path.read_text(encoding="utf-8")
        content = content.replace(
            "script_location = alembic",
            f"script_location = alembic\nversion_locations = %(here)s/{migrations_versions}",
        )
        alembic_ini_path.write_text(content, encoding="utf-8")
        typer.secho(f"Archivo modificado: {alembic_ini_path}", fg=typer.colors.YELLOW)
    else:
        typer.secho(
            f"Advertencia: No se encontró {alembic_ini_path}", fg=typer.colors.RED
        )

    env_py_path = base_path / "alembic" / "env.py"
    if env_py_path.exists():
        content = env_py_path.read_text(encoding="utf-8")
        content = content.replace(
            "from alembic import context",
            f"""from alembic import context
from hexcore.config import LazyConfig
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base
from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import import_all_models
{models_import}

import_all_models(models)
            """,
        )
        content = content.replace(
            "target_metadata = None", "target_metadata = Base.metadata"
        )
        content = content.replace(
            "config = context.config",
            """config = context.config

database_url = LazyConfig().get_config().sql_database_url
if database_url:
    config.set_main_option(\"sqlalchemy.url\", database_url)""",
        )
        env_py_path.write_text(content, encoding="utf-8")
        typer.secho(f"Archivo modificado: {env_py_path}", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"Advertencia: No se encontró {env_py_path}", fg=typer.colors.RED)


def _get_manage_template() -> str:
    return """
from hexcore.infrastructure.cli import app as CLI

if __name__ == "__main__":
    CLI()

"""


def _get_repositories_template(class_name: str) -> str:
    return f"""
from __future__ import annotations
import abc
from uuid import UUID

from hexcore.domain.repositories import IBaseRepository
from .entities import {class_name}


class I{class_name}Repository(IBaseRepository[{class_name}]):
    \"\"\"
    Interfaz del repositorio para la entidad {class_name}.
    \"\"\"
    pass
"""


def _get_services_template(module_name: str, class_name: str) -> str:
    return f"""
from __future__ import annotations
import typing as t
from hexcore.domain.services import BaseDomainService

class {class_name}Service(BaseDomainService):
    \"\"\"
    Servicio de dominio para el módulo {module_name}.
    Orquesta operaciones que no encajan de forma natural en una única entidad.
    \"\"\"
    def __init__(self):
        # Los repositorios y otros servicios se inyectan aquí.
        pass

    # Define aquí los métodos del servicio
"""


def _get_value_objects_template() -> str:
    return """
from __future__ import annotations
from pydantic import BaseModel, ConfigDict
from decimal import Decimal


# Ejemplo de Objeto de Valor
# class MiObjetoDeValor(BaseModel):
#     \"\"\"
#     Un Objeto de Valor (Value Object) de ejemplo.
#     Son inmutables y se definen por sus atributos.
#     \"\"\"
#     valor: str
#
#     model_config = ConfigDict(frozen=True)
"""


def _get_events_template() -> str:
    return """
from __future__ import annotations
from hexcore.domain.events import EntityCreatedEvent


# Ejemplo de Evento de Creacion
# class MiEventoDeCreacion(EntityCreatedEvent[Entidad]):
#     \"\"\"
#     Un Evento de Creación de ejemplo.
#     \"\"\"
#   pass
"""


if __name__ == "__main__":
    app()
