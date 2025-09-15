from _typeshed import Incomplete
from hexcore.config import LazyConfig as LazyConfig

app: Incomplete
PROJECT_ROOT: Incomplete
DOMAIN_PATH: Incomplete
APPLICATION_PATH: Incomplete
INFRAESTRUCTURE_PATH: Incomplete
DB_PATH: Incomplete
MODELS_PATH: Incomplete
DOCUMENTS_PATH: Incomplete
TESTS_DOMAIN_PATH: Incomplete
README: Incomplete
GITIGNORE: Incomplete
MANAGE: Incomplete

def init_project() -> None: ...
def create_domain_module(name: str = ...) -> None: ...
def make_migrations(description: str = ...) -> None: ...
def migrate() -> None: ...
def test(path: str = ..., extra_args: str = ...) -> None: ...
