"""
Persistencia de `drbac` sobre SQLAlchemy. Requiere el extra `[darwin-sqlalchemy]`.

⚠️ **Importar este `__init__` arrastra sqlalchemy.** Lo hace el contenedor, adentro de la
función que resuelve el puerto — mismo criterio que `rbac.orms.sqlalchemy`.
"""
