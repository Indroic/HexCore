import typer

app = typer.Typer()

@app.command(name="ping")
def ping() -> None:
    typer.echo("Pong!")
    
if __name__ == "__main__":
    app()