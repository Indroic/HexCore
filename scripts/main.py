import typer
import pathlib
from .fix_pyi_files import fix_all_pyi
from .fix_types_pyi_aliases import fix_types_pyi_aliases

app = typer.Typer()

@app.command(name="fix-pyi-defaults")
def fix_pyi_defaults_command(root_dir: str = typer.Argument(..., help="Directorio raíz para buscar archivos .pyi")) -> None:
    path = pathlib.Path(root_dir)
    if not path.is_dir():
        typer.echo(f'Error: "{root_dir}" no es un directorio válido.')
        raise typer.Exit(code=1)
    typer.echo(f'Corrigiendo archivos .pyi en "{path.resolve()}"')
    fix_all_pyi(root_dir)
    typer.echo('Archivos .pyi corregidos.')
    
@app.command(name="fix-types-pyi-aliases")
def fix_types_pyi_aliases_command(pyi_path: str = typer.Argument("hexcore/types.pyi", help="Ruta al archivo types.pyi"),
                                  py_path: str = typer.Argument("hexcore/types.py", help="Ruta al archivo types.py")) -> None:
    pyi_file = pathlib.Path(pyi_path)
    py_file = pathlib.Path(py_path)
    if not pyi_file.is_file():
        typer.echo(f'Error: "{pyi_path}" no es un archivo válido.')
        raise typer.Exit(code=1)
    if not py_file.is_file():
        typer.echo(f'Error: "{py_path}" no es un archivo válido.')
        raise typer.Exit(code=1)
    typer.echo(f'Corrigiendo type aliases en "{pyi_file.resolve()}" usando "{py_file.resolve()}"')
    fix_types_pyi_aliases(pyi_file, py_file)
    typer.echo('Type aliases corregidos en types.pyi.')

@app.command(name="ping")
def ping() -> None:
    typer.echo("Pong!")
    
if __name__ == "__main__":
    app()