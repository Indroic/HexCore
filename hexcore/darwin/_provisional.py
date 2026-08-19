"""
Marca de API provisional de Darwin.

Vive en su propio módulo —y no en la fachada— por dos razones: la fachada declara su `__all__`
desde `_EXPORTS`, así que un símbolo definido ahí adentro no quedaría exportado ni aparecería en
el `.pyi` generado; y separarlo permite que la fachada lo importe eager sin arrastrar nada
(esto es stdlib puro).

Sólo stdlib: lo importa `hexcore.darwin`, que tiene que poder importarse sin ningún extra.
"""
from __future__ import annotations

__all__ = ["DarwinProvisionalWarning", "warn_provisional", "reset_provisional_warning"]

#: Fases implementadas y fase en la que la superficie se considera estable.
#:
#: Mientras el borde HTTP (7) no esté cerrado, las formas de `AuthContext`, de los puertos y del
#: emisor de tokens todavía se pueden mover: la Fase 6 cambia el serializer para propagar el
#: actor por la cola, y la 7 define cómo se resuelve el contexto en un request.
CURRENT_PHASE = 4
STABLE_PHASE = 7


class DarwinProvisionalWarning(FutureWarning):
    """
    Darwin es API **provisional**: su superficie puede cambiar sin un bump de major.

    Es `FutureWarning` y no `UserWarning` ni `DeprecationWarning`, y la elección importa:
    `FutureWarning` significa "esto va a cambiar" y se muestra por defecto al usuario final,
    que es exactamente el mensaje. `DeprecationWarning` significa lo contrario —"esto se va"— y
    encima Python lo oculta por defecto, así que nadie lo vería.

    Se puede silenciar de forma quirúrgica, sin apagar otros warnings::

        import warnings

        from hexcore.darwin import DarwinProvisionalWarning

        warnings.filterwarnings("ignore", category=DarwinProvisionalWarning)
    """


#: El aviso se emite **una sola vez por proceso**.
#:
#: Una vez y no por acceso: la fachada se consulta en cada handler, y un warning por atributo
#: sería ruido que entrena a filtrar la categoría entera — con lo cual el aviso deja de cumplir
#: su función. Es el mismo criterio que el aviso de backend sin atomicidad de `rate_limit`.
_ya_avisado = False


def warn_provisional(stacklevel: int = 3) -> None:
    """Emite el aviso, si no se emitió antes en este proceso."""
    global _ya_avisado
    if _ya_avisado:
        return
    _ya_avisado = True

    import warnings

    warnings.warn(
        "hexcore.darwin (Darwin) es API PROVISIONAL: van implementadas las fases "
        f"0-{CURRENT_PHASE} de {STABLE_PHASE}, y la superficie puede cambiar sin un bump de "
        "major hasta que el borde HTTP esté cerrado. Se puede usar —el dominio, la "
        "persistencia y la capa de crypto están completos y testeados— pero fijá la versión "
        "exacta de hexcore si construís sobre él.\n\n"
        "Para silenciarlo:\n\n"
        "    warnings.filterwarnings('ignore', category=DarwinProvisionalWarning)\n",
        DarwinProvisionalWarning,
        stacklevel=stacklevel,
    )


def reset_provisional_warning() -> None:
    """Rearma el aviso. Para tests: sin esto, el primer test que toque Darwin lo consume."""
    global _ya_avisado
    _ya_avisado = False
