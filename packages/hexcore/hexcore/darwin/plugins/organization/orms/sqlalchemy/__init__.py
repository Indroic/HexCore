"""
Persistencia de `organization` sobre SQLAlchemy. Requiere el extra `[darwin-sqlalchemy]`.

Reexporta lo que el consumidor necesita nombrar: los mixins para su paquete ``models/``, los
modelos concretos por si le alcanza el esquema por defecto, y los repositorios.

⚠️ **Importar este `__init__` arrastra sqlalchemy.** Es correcto —es el paquete del backend— pero
significa que nada del núcleo puede importarlo en el nivel superior: lo hace el contenedor, adentro
de la función que resuelve el puerto.
"""
