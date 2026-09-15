"""
`magic_link`: entrar con un link de un solo uso, sin contraseña.

Es el **plugin de referencia**: existe tanto para servir el caso de uso como para demostrar
que el contrato de `DarwinPlugin` alcanza para algo real. Aporta las cuatro cosas que un
plugin típico aporta —una tabla, un router, comandos y un hook— y no necesita ningún mecanismo
que Darwin no tuviera ya.

Cómo funciona, y las tres decisiones que importan:

1. **Reusa la tabla `verification`**, con `purpose="magic_link"`. No aporta tabla propia
   porque no la necesita: el token de un solo uso con vencimiento, techo de intentos y
   `consumed_at` ya está modelado ahí. Un plugin que agrega una tabla equivalente le deja al
   consumidor dos migraciones y dos reapers para el mismo concepto.
2. **El link no autentica solo: canjearlo crea la sesión.** El endpoint que consume el token
   emite un par igual que un sign-in, así que todo lo demás —revocación, rotación,
   transporte, CSRF— funciona sin saber que hubo un magic link.
3. **Pedirlo no dice si la cuenta existe.** `POST /request` responde igual exista o no el
   mail, y la diferencia va en si se manda el mail. Al revés sería un oráculo de enumeración
   en una ruta pública sin autenticación — el peor lugar para tener uno.

Requiere los extras `[darwin]` y `[api]`.

Uso::

    from hexcore.darwin import PluginRegistry, configure_identity
    from hexcore.darwin.plugins.magic_link import MagicLinkPlugin

    plugins = PluginRegistry([MagicLinkPlugin()])
    configure_identity(IdentityConfig(), plugins=plugins)

    app = create_app(
        features=AppFeatures(auth_context=True),
        routers=[build_identity_router(), *plugins.routers()],
    )
"""
from __future__ import annotations

import typing as t
from datetime import timedelta

from hexcore.darwin.domain.plugins import DarwinPlugin, HookBinding

__all__ = ["MagicLinkPlugin", "MAGIC_LINK_PURPOSE", "DEFAULT_TTL"]

#: El `purpose` con el que el plugin guarda sus tokens en `verification`.
#:
#: Es parte de la clave de canje, así que un código emitido para un magic link **no** se puede
#: usar para verificar un mail ni para resetear una contraseña, y al revés tampoco.
MAGIC_LINK_PURPOSE = "magic_link"

#: 15 minutos. Corto a propósito: un magic link es una credencial de portador que viaja por
#: mail y queda en el historial del cliente, en los logs del proveedor y en el buzón. Las 24 h
#: de un token de verificación de mail serían 24 h de ventana para quien acceda a cualquiera
#: de esos tres lugares.
DEFAULT_TTL = timedelta(minutes=15)


class MagicLinkPlugin(DarwinPlugin):
    """
    El plugin. Aporta el router, los comandos y —opcionalmente— un hook de auditoría.

    Args:
        ttl: Vida del link.
        rate_limit: `(intentos, ventana)` para `POST /request`, o `None`. El default limita
            por IP: sin eso, la ruta es un amplificador de mail gratuito contra terceros.
        audit_hook: Si engancha un hook `after` a `magic_link.*` que registra el uso. Está
            apagado por defecto porque el sink de auditoría es opcional; sirve como ejemplo
            mínimo de un hook real.
    """

    name = "magic_link"
    priority = 50

    def __init__(
        self,
        *,
        ttl: timedelta = DEFAULT_TTL,
        rate_limit: tuple[int, int] | None = (3, 900),
        audit_hook: bool = False,
    ) -> None:
        self._ttl = ttl
        self._rate_limit = rate_limit
        self._audit_hook = audit_hook

    # ── Lo que aporta ─────────────────────────────────────────────────────────
    def routers(self) -> t.Sequence[t.Any]:
        from hexcore.darwin.plugins.magic_link.router import build_magic_link_router

        return [
            build_magic_link_router(ttl=self._ttl, rate_limit=self._rate_limit)
        ]

    def register_handlers(self, registry: t.Any) -> None:
        from hexcore.darwin.plugins.magic_link.commands import (
            ConsumeMagicLink,
            ConsumeMagicLinkHandler,
            RequestMagicLink,
            RequestMagicLinkHandler,
        )

        registry.register_command_handler(
            RequestMagicLink, registry.factory(RequestMagicLinkHandler)
        )
        registry.register_command_handler(
            ConsumeMagicLink, registry.factory(ConsumeMagicLinkHandler)
        )

    def hooks(self) -> t.Sequence[HookBinding]:
        if not self._audit_hook:
            return ()

        from hexcore.darwin.plugins.magic_link.commands import registrar_uso

        return [
            HookBinding(
                action="magic_link.*",
                phase="after",
                handler=registrar_uso,
                # Prioridad alta (corre último entre los específicos): auditar quiere ver el
                # resultado final, después de que cualquier otro hook lo haya ajustado.
                priority=900,
            )
        ]
