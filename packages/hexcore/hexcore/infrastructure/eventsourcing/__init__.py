"""
hexcore.infrastructure.eventsourcing - adaptadores del event store.

El paquete no importa nada: cada adaptador vive en su propio modulo y trae sus dependencias.
Asi, `import hexcore.infrastructure.eventsourcing` no puede fallar por un extra que falte, y
quien necesite el de SQLAlchemy lo pide por nombre y recibe el error de `require_extra` con
el `pip install` copiable.

Es el mismo criterio que `hexcore/darwin/infrastructure/orms/__init__.py`, que tambien es
solo docstring.
"""
