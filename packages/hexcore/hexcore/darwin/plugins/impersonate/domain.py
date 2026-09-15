"""
El dominio de `impersonate`: la política de quién puede impersonar a quién, y las excepciones.

**La política es un puerto y no una lista de scopes**, y esa es la decisión de diseño del plugin.
Un scope alcanza para "¿puede impersonar?" y no alcanza para "¿puede impersonar *a esta
persona*?", que es la pregunta que importa: un agente de soporte que puede entrar como cualquier
cliente no debería poder entrar como el CTO. Con un puerto, el consumidor expresa su regla real
—por rol, por organización, por nivel— sin que el plugin invente un lenguaje de permisos.

El default (`ScopeImpersonationPolicy`) cubre el caso común y cierra las tres puertas de escalada
que se repiten en todas las implementaciones que salen mal.
"""
from __future__ import annotations

import abc
import typing as t

from hexcore.darwin.domain.exceptions import AuthorizationError, IdentityError

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.context import AuthContext
    from hexcore.darwin.domain.entities import User

__all__ = [
    "IMPERSONATE_SCOPE",
    "AbstractImpersonationPolicy",
    "ScopeImpersonationPolicy",
    "ImpersonationError",
    "ImpersonationDeniedError",
    "ImpersonationChainError",
    "ImpersonationSelfError",
    "ImpersonationTargetProtectedError",
    "ImpersonationNotActiveError",
    "IMPERSONATE_EXCEPTION_STATUS_MAP",
]

#: El scope que exige la política por default.
#:
#: Un scope y no un rol: el rol se puede tener por herencia sin que nadie lo haya pensado, y
#: `RoleRegistry` resuelve la herencia transitiva. Un scope explícito en el token deja el permiso
#: visible en el token mismo, que es donde alguien lo va a buscar durante un incidente.
IMPERSONATE_SCOPE = "identity:impersonate"


class AbstractImpersonationPolicy(abc.ABC):
    """
    Decide si un actor puede impersonar a un sujeto.

    Uso, para una política propia::

        class SoloClientes(AbstractImpersonationPolicy):
            def authorize(self, *, context, subject):
                if "staff" in subject.extra.get("roles", []):
                    raise ImpersonationTargetProtectedError("No se impersona al staff.")
    """

    @abc.abstractmethod
    def authorize(
        self, *, context: "AuthContext[t.Any]", subject: "User"
    ) -> None:
        """
        Autoriza o lanza.

        Lanza en vez de devolver un booleano porque el motivo del rechazo tiene que llegar al
        usuario y a la auditoría: un `False` obliga al llamador a inventar un mensaje, y el
        mensaje inventado es siempre el genérico.

        Raises:
            ImpersonationDeniedError o una subclase.
        """


class ScopeImpersonationPolicy(AbstractImpersonationPolicy):
    """
    La política por default: un scope, más las tres puertas de escalada cerradas.

    Args:
        scope: El scope que el **actor** tiene que tener.
        protect_impersonators: Si un sujeto que también puede impersonar está protegido.
            **Encendido por default**, y es la puerta que más importa: sin eso, un operador entra
            como otro operador y desde ahí impersona a cualquiera, con la auditoría diciendo que
            fue el segundo. Es escalada lateral con la traza borrada.
        protected_scopes: Scopes que, si el sujeto los tiene, lo hacen inimpersonable. Poné acá
            lo que gobierna tu sistema (`"admin"`, `"billing:write"`): impersonar a quien puede
            aprobar pagos es la ruta corta al fraude.
    """

    def __init__(
        self,
        *,
        scope: str = IMPERSONATE_SCOPE,
        protect_impersonators: bool = True,
        protected_scopes: t.Sequence[str] = (),
    ) -> None:
        self._scope = scope
        self._protect_impersonators = protect_impersonators
        self._protected = tuple(protected_scopes)

    def authorize(self, *, context: "AuthContext[t.Any]", subject: "User") -> None:
        # 1. No se impersona en cadena. Si A está impersonando a B y desde ahí impersona a C, la
        #    auditoría de la segunda dice que el actor es B — que nunca hizo nada. Es la forma
        #    más barata de borrar la traza, y por eso se corta antes que cualquier otro chequeo.
        if context.is_impersonating:
            raise ImpersonationChainError(
                "No se puede impersonar mientras ya estás impersonando a alguien. Terminá la "
                "impersonación actual primero: en cadena, la auditoría señalaría al intermedio "
                "y no a vos."
            )

        # 2. No se impersona a uno mismo. No es peligroso, pero produce una sesión impersonada
        #    con actor == subject, que es exactamente el estado que `AuthContext` prohíbe: el
        #    error acá es claro y el de más adelante sería un `ValueError` del validador.
        if context.actor_id == subject.id:
            raise ImpersonationSelfError(
                "No tiene sentido impersonarte a vos mismo: ya tenés tu propia sesión."
            )

        # 3. El actor necesita el permiso, y se consulta del **actor** — nunca del subject. Es
        #    la regla que `AuthContext.has_scope` ya garantiza, y el motivo por el que esa
        #    consulta no mira al subject en ningún caso.
        if not context.has_scope(self._scope):
            raise ImpersonationDeniedError(
                f"Falta el scope {self._scope!r} para impersonar."
            )

        # 4. El sujeto puede estar protegido.
        scopes_del_sujeto = _scopes_de(subject)

        if self._protect_impersonators and self._scope in scopes_del_sujeto:
            raise ImpersonationTargetProtectedError(
                "No se puede impersonar a alguien que también puede impersonar: sería escalada "
                "lateral con la auditoría apuntando a la persona equivocada."
            )

        chocan = sorted(set(self._protected) & scopes_del_sujeto)
        if chocan:
            raise ImpersonationTargetProtectedError(
                f"El sujeto tiene scopes protegidos ({', '.join(chocan)}), así que no se puede "
                f"impersonar."
            )


def _scopes_de(usuario: "User") -> set[str]:
    """
    Los scopes del usuario, sacados de `extra`.

    De `extra` y no de una tabla propia porque los permisos son del consumidor: el framework no
    tiene un modelo de autorización, tiene `RoleRegistry` y este campo. Un valor que no es una
    lista de strings se trata como vacío en vez de explotar — un `extra` mal formado no debería
    convertirse en un 500 en el camino de la autorización, y "vacío" falla cerrando.
    """
    crudo = usuario.extra.get("scopes")
    if not isinstance(crudo, (list, tuple, set, frozenset)):
        return set()
    return {str(x) for x in t.cast("t.Iterable[t.Any]", crudo)}


# ── Las excepciones ───────────────────────────────────────────────────────────
class ImpersonationError(IdentityError):
    """Base de las fallas del plugin."""


class ImpersonationDeniedError(AuthorizationError):
    """
    El actor no tiene permiso para impersonar. 403.

    403 y no 404: el actor **está** autenticado, y lo que falta es autorización. Un 404 acá
    escondería el endpoint a costa de que un operador legítimo sin el scope no entienda qué le
    falta.
    """


class ImpersonationChainError(ImpersonationDeniedError):
    """
    Se intentó impersonar estando ya impersonando. 403.

    Ver el punto 1 de `ScopeImpersonationPolicy`: en cadena, la auditoría señala al intermedio.
    """


class ImpersonationSelfError(ImpersonationError):
    """Se intentó impersonar a uno mismo. 409: no es una falla de permisos, es un sinsentido."""


class ImpersonationTargetProtectedError(ImpersonationDeniedError):
    """El sujeto está protegido. 403. Ver `ScopeImpersonationPolicy`."""


class ImpersonationNotActiveError(ImpersonationError):
    """
    Se intentó terminar una impersonación en una sesión que no lo es. 409.

    Se distingue de "no autorizado" a propósito: es un error del cliente que llamó al endpoint
    equivocado, no un intento de escalada, y confundirlos llena la auditoría de falsos positivos.
    """


#: El mapa que el plugin aporta vía `exception_status_map()`.
IMPERSONATE_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    ImpersonationDeniedError: 403,
    ImpersonationChainError: 403,
    ImpersonationTargetProtectedError: 403,
    ImpersonationSelfError: 409,
    ImpersonationNotActiveError: 409,
    # `ImpersonationError` (la base) **no** se mapea, por lo mismo que el núcleo no mapea
    # `IdentityError`: `_specificity` ordena por profundidad de MRO, así que mapearla haría que
    # una falla nueva se tragara con ese status en vez de aparecer como un 500 en los tests.
}
