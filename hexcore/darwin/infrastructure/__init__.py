"""
Capa de infraestructura de Darwin. Requiere el extra ``[darwin]``.

Vacío a propósito, igual que el resto del paquete: los imports van por la fachada
`hexcore.darwin`, que resuelve perezosamente. Un re-export acá haría que importar
cualquier submódulo arrastrase sqlalchemy, y `tests/test_optional_dependencies.py` lo
detectaría.
"""
