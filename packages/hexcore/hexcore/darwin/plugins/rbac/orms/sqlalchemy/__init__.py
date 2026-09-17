"""
Persistencia de `rbac` sobre SQLAlchemy. Requiere el extra `[darwin-sqlalchemy]`.

⚠️ **Importar este `__init__` arrastra sqlalchemy.** Es correcto —es el paquete del backend—
pero significa que nada del núcleo puede importarlo en el nivel superior: lo hace el
contenedor, adentro de la función que resuelve el puerto.
"""
