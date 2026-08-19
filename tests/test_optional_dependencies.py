import subprocess
import sys
import pytest

@pytest.mark.parametrize("module_to_hide, module_to_import", [
    ("sqlalchemy", "hexcore.infrastructure.uow"),
    ("beanie", "hexcore.infrastructure.uow"),
    ("sqlalchemy", "hexcore.infrastructure.repositories.implementations"),
    ("beanie", "hexcore.infrastructure.repositories.implementations"),
    ("sqlalchemy", "hexcore.infrastructure.repositories.utils"),
    ("beanie", "hexcore.infrastructure.repositories.utils"),
    # Los scopes de F3 no deben arrastrar SQLAlchemy en import time.
    ("sqlalchemy", "hexcore.infrastructure.uow.scopes"),
    # El módulo de contexto y el de resolución de FQN son stdlib puro.
    ("sqlalchemy", "hexcore.domain.cqrs.context"),
    ("sqlalchemy", "hexcore.domain.cqrs.resolution"),
    ("fastapi", "hexcore.domain.cqrs"),
    ("fastapi", "hexcore.application.cqrs"),
    # ── Darwin: el dominio de identidad es stdlib + pydantic y nada más ────────
    # La fachada resuelve perezosamente, así que importarla no puede arrastrar ningún
    # extra. Y el contexto lo importan el middleware de CQRS y los command handlers, o sea
    # que tiene que funcionar en un proceso sin `[sql]`, sin `[api]` y sin `[darwin]`.
    ("sqlalchemy", "hexcore.darwin"),
    ("fastapi", "hexcore.darwin"),
    ("joserfc", "hexcore.darwin"),
    ("argon2", "hexcore.darwin"),
    ("sqlalchemy", "hexcore.darwin.domain.context"),
    ("fastapi", "hexcore.darwin.domain.context"),
    ("sqlalchemy", "hexcore.darwin.domain.exceptions"),
    ("sqlalchemy", "hexcore.darwin.domain.value_objects"),
    ("sqlalchemy", "hexcore.darwin.domain.permissions"),
    ("sqlalchemy", "hexcore.darwin.domain.ports"),
    ("sqlalchemy", "hexcore.darwin.domain.entities"),
    ("sqlalchemy", "hexcore.darwin.domain.events"),
    # La fachada expone la capa de persistencia, pero resuelve perezosamente: nombrarla
    # no puede arrastrar sqlalchemy hasta que pidas un modelo.
    ("joserfc", "hexcore.darwin.domain.entities"),
    ("argon2", "hexcore.darwin.domain.ports"),
    # El reloj y la revocación son de infraestructura pero no tocan crypto ni sqlalchemy:
    # los importa el middleware de CQRS y tienen que funcionar en un proceso pelado.
    ("joserfc", "hexcore.darwin.infrastructure.clock"),
    ("argon2", "hexcore.darwin.infrastructure.clock"),
    ("sqlalchemy", "hexcore.darwin.infrastructure.clock"),
    ("joserfc", "hexcore.darwin.infrastructure.revocation"),
    ("argon2", "hexcore.darwin.infrastructure.revocation"),
    ("sqlalchemy", "hexcore.darwin.infrastructure.revocation"),
    # La configuración la importa `hexcore.config`, que importa medio framework: tiene que
    # ser stdlib + pydantic y nada más.
    ("sqlalchemy", "hexcore.darwin.application.config"),
    ("fastapi", "hexcore.darwin.application.config"),
    ("joserfc", "hexcore.darwin.application.config"),
    ("argon2", "hexcore.darwin.application.config"),
    # El contenedor resuelve sus adaptadores perezosamente, así que importarlo no puede
    # arrastrar ninguno de los extras que esos adaptadores necesitan.
    ("sqlalchemy", "hexcore.darwin.application.container"),
    ("joserfc", "hexcore.darwin.application.container"),
    ("argon2", "hexcore.darwin.application.container"),
    ("fastapi", "hexcore.darwin.application.container"),
    # El sobre que cruza la cola: lo importa `configure_identity`, y su restaurador lo
    # invoca el `CQRSConsumer`, que corre en workers sin extras de API. El códec es
    # `hmac` + `json` de la stdlib a propósito y no joserfc: el sobre no es un JWT.
    ("sqlalchemy", "hexcore.darwin.infrastructure.envelope"),
    ("joserfc", "hexcore.darwin.infrastructure.envelope"),
    ("argon2", "hexcore.darwin.infrastructure.envelope"),
    ("fastapi", "hexcore.darwin.infrastructure.envelope"),
    # El punto de extensión del núcleo lo importan el serializer y los cinco transportes.
    # (No se chequea contra pydantic: no es opcional — `hexcore/__init__.py` lo importa
    # eager, así que esconderlo rompe cualquier import del paquete.)
    ("sqlalchemy", "hexcore.domain.cqrs.envelope"),
    ("fastapi", "hexcore.domain.cqrs.envelope"),
])
def test_optional_dependencies_do_not_crash_imports(module_to_hide, module_to_import):
    """
    Este test verifica que si una dependencia opcional como SQLAlchemy o Beanie no está
    instalada en el entorno (simulado bloqueando su importación), la carga de los
    módulos base de infraestructura de HexCore no fallará con ImportError.
    """
    
    # El código inyectado bloquea la importación del módulo objetivo y luego intenta importar HexCore.
    code = f"""
import sys

class MockFinder:
    @classmethod
    def find_spec(cls, fullname, path, target=None):
        if fullname.split(".")[0] == "{module_to_hide}":
            raise ImportError("Mocked ImportError for " + fullname)
        return None

sys.meta_path.insert(0, MockFinder)

# Si esto no lanza ImportError, el test pasa.
import {module_to_import}
print("Imported {module_to_import} successfully without {module_to_hide}")
"""

    import os
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env
    )
    
    assert result.returncode == 0, f"Import falló: {result.stderr}"
    assert "Imported" in result.stdout
