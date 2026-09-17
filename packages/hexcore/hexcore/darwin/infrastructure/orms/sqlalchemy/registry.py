"""
Resolución del modelo concreto de cada tabla de identidad, **sin importar `models.py`**.

Este módulo existe por un modo de falla concreto, y vale la pena escribirlo entero porque el
síntoma no dice nada de la causa.

`models.py` declara las seis clases concretas sobre `Base` y declararlas es lo que las registra
en `Base.metadata`. Hasta acá, bien: es su trabajo, y su docstring lo dice. El problema era
**cómo lo conseguían los repositorios**. Cada `_modelo_por_defecto()` hacía::

    from hexcore.darwin.infrastructure.orms.sqlalchemy.models import SessionModel

y ese import no trae `SessionModel`: ejecuta el módulo entero, que declara **las seis**. Si el
consumidor ya declaró su propio modelo de usuario sobre ``darwin_user`` a partir de `UserMixin`
—el camino que la documentación recomienda, y el único que permite agregarle columnas— entonces
hay dos clases peleando por la misma tabla en el mismo `MetaData`, y SQLAlchemy corta con::

    InvalidRequestError: Table 'darwin_user' is already defined for this MetaData instance.
    Specify 'extend_existing=True' to redefine options and columns on an existing Table object.

El error aparece al **primer uso de cualquier repositorio**, no al declarar el modelo, así que
el stack trace apunta a una consulta de sesiones y no al import que la causó. Y la sugerencia
que trae —`extend_existing=True`— es activamente mala acá: haría que la última clase declarada
le pise las columnas a la primera, de forma silenciosa y dependiente del orden de importación.

La salida es no importar `models.py` cuando el consumidor ya tiene lo suyo. Eso es lo que hace
`identity_model`, en tres pasos y en este orden:

1. **Lo que el consumidor declaró en `IdentityConfig`** (`user_model`, `session_model`, …). Es
   explícito, así que gana siempre.
2. **Una clase ya mapeada que componga el mixin.** Cubre al consumidor que declaró sus modelos
   y no configuró nada, que es el caso mayoritario y el que hoy rompe. No se busca por nombre de
   tabla sino por mixin, porque renombrar la tabla vía `__tablename__` es un caso soportado y
   buscar por nombre lo dejaría afuera.
3. **Recién ahí, el default de `models.py`.** Con los pasos 1 y 2 en falso, no hay ninguna clase
   compitiendo y el import es seguro.

El paso 2 recorre las subclases de `Base` en vez de `Base.registry.mappers`, y la diferencia
importa: tocar `.mappers` dispara `configure_mappers()`, que resuelve todas las relaciones del
proyecto y puede explotar por una relación del consumidor a medio declarar. Resolver un modelo
de identidad no tiene por qué configurar el ORM entero, y hacerlo convertiría este módulo en
una fuente nueva de errores en el arranque.
"""
from __future__ import annotations

import typing as t

__all__ = [
    "IdentityModelKind",
    "MIXIN_POR_TIPO",
    "identity_model",
    "resolve_identity_models",
]

#: Las seis tablas del esquema de identidad, por nombre corto.
IdentityModelKind = t.Literal[
    "user", "session", "account", "verification", "audit", "jwks"
]

#: Nombre del mixin que define cada tabla, y nombre de la clase concreta por defecto en
#: `models.py`. Se guardan como **strings** y no como clases para que importar este módulo no
#: arrastre `models_mixins` ni `models`: el punto entero es postergar esos imports.
MIXIN_POR_TIPO: dict[str, tuple[str, str]] = {
    "user": ("UserMixin", "UserModel"),
    "session": ("SessionMixin", "SessionModel"),
    "account": ("AccountMixin", "AccountModel"),
    "verification": ("VerificationMixin", "VerificationModel"),
    "audit": ("AuditLogMixin", "AuditLogModel"),
    "jwks": ("JwksMixin", "JwksModel"),
}


def _subclases_mapeadas(base: type) -> list[type]:
    """
    Todas las subclases concretas de `Base`, recursivamente.

    "Concreta" acá es "tiene `__table__`": una clase declarativa abstracta (`__abstract__`) o un
    mixin intermedio no lo tiene, y contarla daría una ambigüedad inventada.

    Recorre `__subclasses__()` y no el registry de SQLAlchemy porque no queremos disparar
    `configure_mappers()` — ver el docstring del módulo.
    """
    encontradas: list[type] = []
    pendientes = list(base.__subclasses__())
    vistas: set[int] = set()

    while pendientes:
        cls = pendientes.pop()
        if id(cls) in vistas:
            continue
        vistas.add(id(cls))
        pendientes.extend(cls.__subclasses__())
        if getattr(cls, "__table__", None) is not None:
            encontradas.append(cls)

    return encontradas


def identity_model(kind: IdentityModelKind) -> type:
    """
    El modelo concreto de esa tabla, sin importar `models.py` si hay uno del consumidor.

    Args:
        kind: Cuál de las seis tablas. Ver `MIXIN_POR_TIPO`.

    Raises:
        LookupError: si hay dos o más clases componiendo el mixin y **ninguna** está sobre la
            tabla canónica. No se elige una por orden de declaración a propósito: el orden
            depende de qué módulo importó qué primero, así que la elección cambiaría entre
            correr los tests y correr la app. El mensaje las nombra a todas y dice qué campo de
            `IdentityConfig` desempata.

    Uso::

        from hexcore.darwin.infrastructure.orms.sqlalchemy.registry import identity_model

        modelo = identity_model("session")
    """
    nombre_mixin, nombre_default = MIXIN_POR_TIPO[kind]

    from hexcore.darwin.infrastructure.orms.sqlalchemy import models_mixins
    from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

    mixin = getattr(models_mixins, nombre_mixin)
    tabla_default = getattr(models_mixins, f"DEFAULT_{kind.upper()}_TABLE")

    # Sólo las clases cuya tabla sigue **viva en el metadata**. Una clase declarada y después
    # retirada con `Base.metadata.remove()` —lo que hace todo test que declara un modelo de
    # prueba— sigue apareciendo en `Base.__subclasses__()` hasta que la recolecta el GC, y
    # tomarla como candidata haría que el resultado dependa del momento de la recolección.
    vivas = Base.metadata.tables
    candidatas = [
        c
        for c in _subclases_mapeadas(Base)
        if issubclass(c, mixin) and getattr(c, "__tablename__", None) in vivas
    ]

    # Primero, la que está sobre la tabla canónica. El nombre de la tabla es la identidad de la
    # tabla, y el mixin solo no alcanza para desempatar: cualquier clase del proyecto que
    # componga `UserMixin` —un modelo de otra app, una clase de test, un modelo histórico que
    # quedó mapeado— es candidata por mixin y no tiene nada que ver con la identidad. Filtrar
    # por `__tablename__` deja una sola en el caso masivo, que es el consumidor agregándole
    # columnas a `darwin_user`.
    #
    # No puede haber dos acá: dos clases sobre la misma tabla en el mismo `MetaData` ya habrían
    # hecho fallar la declaración con `InvalidRequestError`.
    en_la_tabla = [c for c in candidatas if getattr(c, "__tablename__", None) == tabla_default]
    if en_la_tabla:
        return en_la_tabla[0]

    # Sin nadie en la tabla canónica, la única que compone el mixin es la del consumidor que la
    # renombró vía `__tablename__`, que es un caso soportado.
    if len(candidatas) == 1:
        return candidatas[0]

    if len(candidatas) > 1:
        # `getattr` y no `c.__tablename__`: el atributo lo agrega el mapeo declarativo en
        # tiempo de ejecución, así que no está en el tipo estático de una `type` cualquiera.
        # Mismo criterio que `identity_tables` en `schema.py`.
        detalle = ", ".join(
            f"{c.__module__}.{c.__qualname__} (tabla '{getattr(c, '__tablename__', '?')}')"
            for c in sorted(candidatas, key=lambda c: c.__qualname__)
        )
        raise LookupError(
            f"Hay {len(candidatas)} clases mapeadas que componen `{nombre_mixin}`, ninguna "
            f"sobre '{tabla_default}', y Darwin no puede saber cuál es la de identidad: "
            f"{detalle}.\n\n"
            f"Elegir por orden de declaración haría que la respuesta dependa de qué módulo se "
            f"importó primero, o sea que cambie entre los tests y la app. Declarala explícita:\n\n"
            f"    IdentityConfig({kind}_model=TuModelo)\n"
        )

    # Ninguna clase del consumidor compone el mixin: importar el default es seguro, porque no
    # hay nada con quién colisionar.
    from hexcore.darwin.infrastructure.orms.sqlalchemy import models

    return t.cast(type, getattr(models, nombre_default))


def resolve_identity_models(config: t.Any = None) -> list[type]:
    """
    Los seis modelos concretos que este despliegue usa de verdad.

    Es lo que hay que comparar contra `Base.metadata` para detectar la tabla que le falta al
    `env.py` de Alembic. Usar `IDENTITY_MODELS` para eso era incorrecto por partida doble:
    importaba `models.py` —con la colisión que describe el docstring del módulo— y además
    verificaba las tablas **por defecto** en vez de las que el consumidor declaró, así que a
    quien renombró una tabla le avisaba de una que no existe y le tapaba la que sí.

    Args:
        config: Un `IdentityConfig`, del que se leen los campos `<kind>_model`. `None` salta
            el paso 1 de la resolución.
    """
    resueltos: list[type] = []
    for kind in MIXIN_POR_TIPO:
        declarado = (
            getattr(config, f"{kind}_model", None) if config is not None else None
        )
        if declarado is not None:
            resueltos.append(t.cast(type, declarado))
            continue
        resueltos.append(identity_model(t.cast(IdentityModelKind, kind)))
    return resueltos
