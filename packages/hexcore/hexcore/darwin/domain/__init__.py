"""
Capa de dominio de Darwin: stdlib + pydantic, nada más.

Sin sqlalchemy, sin fastapi, sin crypto. Es lo que permite que un middleware de CQRS o un
command handler importen `AuthContext` sin arrastrar ningún extra.

Vacío a propósito, igual que `hexcore.darwin`: los imports van por la fachada.
"""
