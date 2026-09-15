"""
`impersonate`: entrar como otro usuario, sin magia negra y con todo auditado.

Es el plugin que justifica la decisión más importante del diseño de Darwin: que `AuthContext`
tenga **dos** principales —`actor`, quien ejecuta, y `subject`, a quién afecta— en vez de un
`user_id` con un flag al costado. Con eso, una impersonación es una sesión normal con los dos
campos distintos, y todo lo demás —revocación, transporte, CSRF, auditoría, el sobre que cruza la
cola— funciona sin saber que hay una impersonación en curso.

**No aporta ninguna tabla.** La fila de `session` ya lleva `actor_user_id`, `subject_user_id`,
`impersonation_reason`, `impersonation_granted_by` e `impersonation_expires_at`: la impersonación
está modelada en el esquema desde la Fase 3, justamente para que este plugin no tenga que
inventar nada.

Lo que el plugin agrega es la **autorización** —quién puede impersonar a quién— y los dos
endpoints. La política es un puerto: un scope alcanza para "¿puede impersonar?" y no para "¿puede
impersonar *a esta persona*?", que es la pregunta que importa.

Las cuatro propiedades que hacen que esto sea auditable y no un agujero:

1. **La sesión del operador no se toca.** Terminar es descartar el token de impersonación.
2. **Techo de 60 minutos, no renovable.** Lo hace real el rechazo del refresh en el núcleo: una
   sesión impersonada no se rota, así que el techo no se puede estirar.
3. **No hay cadenas.** Impersonar estando impersonando dejaría la auditoría apuntando al
   intermedio, que nunca hizo nada.
4. **`has_scope` consulta al actor, nunca al subject.** Impersonar no presta permisos.

Requiere los extras `[darwin]` y `[api]`. Depende del sobre firmado de la Fase 6 para que el actor
cruce la cola: un comando encolado durante una impersonación se procesa en el worker con los dos
principales correctos, y no con el subject solo.

Uso::

    from hexcore.darwin import PluginRegistry, configure_identity
    from hexcore.darwin.plugins.impersonate import (
        ImpersonatePlugin,
        ScopeImpersonationPolicy,
    )

    plugins = PluginRegistry([
        ImpersonatePlugin(
            policy=ScopeImpersonationPolicy(protected_scopes=["admin", "billing:write"])
        )
    ])
    configure_identity(config, plugins=plugins)

    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True),
        routers=[build_identity_router(), *plugins.routers()],
    )
"""
from __future__ import annotations

import threading
import typing as t

from hexcore.darwin.domain.plugins import DarwinPlugin
from hexcore.darwin.plugins.impersonate.domain import (
    IMPERSONATE_EXCEPTION_STATUS_MAP,
    IMPERSONATE_SCOPE,
    AbstractImpersonationPolicy,
    ImpersonationChainError,
    ImpersonationDeniedError,
    ImpersonationError,
    ImpersonationNotActiveError,
    ImpersonationSelfError,
    ImpersonationTargetProtectedError,
    ScopeImpersonationPolicy,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.plugins.impersonate.service import ImpersonationService

__all__ = [
    "ImpersonatePlugin",
    "AbstractImpersonationPolicy",
    "ScopeImpersonationPolicy",
    "IMPERSONATE_SCOPE",
    "ImpersonationError",
    "ImpersonationDeniedError",
    "ImpersonationChainError",
    "ImpersonationSelfError",
    "ImpersonationTargetProtectedError",
    "ImpersonationNotActiveError",
    "IMPERSONATE_EXCEPTION_STATUS_MAP",
    "get_impersonation_service",
]


class ImpersonatePlugin(DarwinPlugin):
    """
    El plugin de impersonación.

    Args:
        policy: Quién puede impersonar a quién. Por defecto `ScopeImpersonationPolicy()`, que
            exige el scope y cierra las tres puertas de escalada. **Pasá la tuya** si tu modelo de
            permisos no es por scopes: es el punto de extensión principal del plugin.
        include_router: Si aporta su router.
        rate_limit: `(intentos, ventana)` para iniciar. Existe aunque la ruta esté autenticada: si
            un operador queda comprometido, el límite convierte "impersonar a toda la base" en
            algo que tarda y se nota.
    """

    name = "impersonate"

    #: Corre después de `two_factor` (20) y `oauth` (30): la impersonación es sobre una sesión ya
    #: establecida, así que su lugar en el orden es después de todo lo que autentica.
    priority = 60

    def __init__(
        self,
        *,
        policy: AbstractImpersonationPolicy | None = None,
        include_router: bool = True,
        rate_limit: tuple[int, int] | None = (10, 300),
    ) -> None:
        self._policy = policy
        self._include_router = include_router
        self._rate_limit = rate_limit
        self._lock = threading.RLock()
        self._service: "ImpersonationService | None" = None

    # ── El servicio ───────────────────────────────────────────────────────────
    def service(self) -> "ImpersonationService":
        """
        El servicio, construido perezosamente desde el contenedor de identidad.

        Perezoso y cacheado con `RLock`, igual que los proveedores del contenedor: el plugin se
        instancia al declarar el registro —antes de `configure_identity`— así que construirlo en
        `__init__` obligaría a un orden de cableado que nadie tiene por qué recordar.
        """
        with self._lock:
            if self._service is None:
                from hexcore.darwin.application.container import get_identity_container
                from hexcore.darwin.plugins.impersonate.service import (
                    ImpersonationService,
                )

                contenedor = get_identity_container()
                self._service = ImpersonationService(
                    users=contenedor.users(),
                    sessions_repository=contenedor.sessions_repository(),
                    sessions=contenedor.session_service(),
                    clock=contenedor.clock(),
                    policy=self._policy,
                    audit=_sink_de(contenedor),
                )
            return self._service

    def reset(self) -> None:
        """Descarta el servicio cacheado. Para los tests, que reconfiguran el contenedor."""
        with self._lock:
            self._service = None

    # ── Lo que aporta ─────────────────────────────────────────────────────────
    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        return IMPERSONATE_EXCEPTION_STATUS_MAP

    def routers(self) -> t.Sequence[t.Any]:
        if not self._include_router:
            return ()

        from hexcore.darwin.plugins.impersonate.router import build_impersonate_router

        return [build_impersonate_router(rate_limit=self._rate_limit)]

    def startup_steps(self) -> t.Sequence[t.Any]:
        """
        Un paso que avisa si no hay sink de auditoría.

        Avisa y no falla: la auditoría es opcional en el resto de Darwin, y convertirla en
        obligatoria acá haría que agregar el plugin rompa un despliegue que funciona. Pero una
        impersonación sin auditoría es exactamente lo que este plugin promete que no pasa, así que
        el aviso tiene que estar en el arranque y no en un docstring.
        """
        return [_AvisarSinAuditoria()]

    def register_handlers(self, registry: t.Any) -> None:
        from hexcore.darwin.plugins.impersonate.commands import (
            StartImpersonation,
            StartImpersonationHandler,
            StopImpersonation,
            StopImpersonationHandler,
        )

        registry.register_command_handler(
            StartImpersonation, registry.factory(StartImpersonationHandler)
        )
        registry.register_command_handler(
            StopImpersonation, registry.factory(StopImpersonationHandler)
        )


class _AvisarSinAuditoria:
    """`StartupStep` que revisa que haya sink de auditoría."""

    name = "darwin.impersonate.audit_check"

    async def __call__(self) -> None:
        import logging

        from hexcore.darwin.application.container import get_identity_container

        if _sink_de(get_identity_container()) is None:
            logging.getLogger("hexcore.darwin.impersonate").warning(
                "El plugin 'impersonate' está registrado y no hay sink de auditoría cableado. "
                "Cada impersonación va a funcionar y **no va a quedar registrada**, que es "
                "justamente lo que este plugin existe para evitar. Pasá "
                "`audit=SqlAlchemyAuditSink()` a `configure_identity`."
            )


def _sink_de(contenedor: t.Any) -> t.Any:
    """
    El sink de auditoría del contenedor, o `None`.

    Se lee por `getattr` porque el contenedor no lo expone como proveedor público: es opcional y
    su único consumidor son los servicios que lo reciben inyectado. Leerlo así es la concesión
    que evita agregarle una propiedad al contenedor sólo para este plugin.
    """
    return getattr(contenedor, "_audit", None)


def get_impersonation_service() -> "ImpersonationService":
    """
    El servicio del plugin registrado en este despliegue.

    Se busca en el registro del contenedor y no en un global propio: los plugins son de un
    despliegue, y un segundo global tendría que resetearse aparte en cada test.

    Raises:
        RuntimeError: el plugin no está registrado, con la remediación copiable.
    """
    from hexcore.darwin.application.container import get_identity_container

    plugin = get_identity_container().plugins.get(ImpersonatePlugin.name)
    if not isinstance(plugin, ImpersonatePlugin):
        raise RuntimeError(
            "El plugin 'impersonate' no está registrado en este despliegue.\n\n"
            "    from hexcore.darwin import PluginRegistry, configure_identity\n"
            "    from hexcore.darwin.plugins.impersonate import ImpersonatePlugin\n\n"
            "    configure_identity(config, plugins=PluginRegistry([ImpersonatePlugin()]))"
        )
    return plugin.service()
