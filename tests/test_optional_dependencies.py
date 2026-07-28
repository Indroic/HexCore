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
