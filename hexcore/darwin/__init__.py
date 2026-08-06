"""
Darwin — el módulo de identidad nativo de HexCore.

Port de la arquitectura, el esquema y el sistema de plugins de Better Auth a Python + CQRS.

Un import obvio para todo lo de auth::

    from hexcore.darwin import AuthContext, current_auth, require_auth

Misma forma que `hexcore.cqrs`, `hexcore.sql` y `hexcore.fastapi`: `_EXPORTS` más un
`__getattr__` de módulo (PEP 562) que resuelve al primer acceso y cachea.

La fachada vive acá, en el `__init__` del paquete, y no en un `hexcore/darwin.py` aparte:
un módulo y un paquete con el mismo nombre no pueden coexistir —el paquete gana y el módulo
queda muerto—, así que el `__init__` **es** la fachada.

Importar esto **no** arrastra las dependencias de `[darwin]`: sólo se importa el submódulo
del símbolo que pedís, así que `from hexcore.darwin import AuthContext` no toca joserfc, ni
argon2, ni sqlalchemy. Eso es lo que verifica `tests/test_optional_dependencies.py`, y es la
razón de que este archivo no tenga ni un `from .domain import ...` en el nivel superior.

El tipado lo aporta `hexcore/darwin/__init__.pyi`, **generado** desde este `_EXPORTS` por
`scripts/gen_stubs.py`. Sin el stub, `__all__ = sorted(_EXPORTS)` y `__getattr__` son
expresiones de runtime y todo lo de acá tiparía `Any`. No edites el `.pyi` a mano: agregá la
entrada acá y corré ``uv run python scripts/gen_stubs.py --write``.
"""
from __future__ import annotations

import importlib
import typing as t

_EXPORTS: dict[str, tuple[str, str]] = {
    # ── Contexto: quién ejecuta vs a quién afecta ──────────────────────────────
    "AuthContext": ("hexcore.darwin.domain.context", "AuthContext"),
    "Principal": ("hexcore.darwin.domain.context", "Principal"),
    "SystemPrincipal": ("hexcore.darwin.domain.context", "SystemPrincipal"),
    "Impersonation": ("hexcore.darwin.domain.context", "Impersonation"),
    "Transport": ("hexcore.darwin.domain.context", "Transport"),
    "AUTH_CONTEXT": ("hexcore.darwin.domain.context", "AUTH_CONTEXT"),
    "current_auth": ("hexcore.darwin.domain.context", "current_auth"),
    "require_auth": ("hexcore.darwin.domain.context", "require_auth"),
    "auth_scope": ("hexcore.darwin.domain.context", "auth_scope"),
    "system_context": ("hexcore.darwin.domain.context", "system_context"),
    # ── Value objects ─────────────────────────────────────────────────────────
    "Email": ("hexcore.darwin.domain.value_objects", "Email"),
    "AccessTokenClaims": ("hexcore.darwin.domain.value_objects", "AccessTokenClaims"),
    "TokenPair": ("hexcore.darwin.domain.value_objects", "TokenPair"),
    "TokenType": ("hexcore.darwin.domain.value_objects", "TokenType"),
    "VerificationPurpose": (
        "hexcore.darwin.domain.value_objects",
        "VerificationPurpose",
    ),
    # ── Entidades ─────────────────────────────────────────────────────────────
    "User": ("hexcore.darwin.domain.entities", "User"),
    "IdentitySession": ("hexcore.darwin.domain.entities", "IdentitySession"),
    "Account": ("hexcore.darwin.domain.entities", "Account"),
    "Verification": ("hexcore.darwin.domain.entities", "Verification"),
    "CREDENTIAL_PROVIDER": ("hexcore.darwin.domain.entities", "CREDENTIAL_PROVIDER"),
    # ── Roles y permisos ──────────────────────────────────────────────────────
    "Permission": ("hexcore.darwin.domain.permissions", "Permission"),
    "Role": ("hexcore.darwin.domain.permissions", "Role"),
    "RoleRegistry": ("hexcore.darwin.domain.permissions", "RoleRegistry"),
    "PermissionCycleError": (
        "hexcore.darwin.domain.permissions",
        "PermissionCycleError",
    ),
    "default_registry": ("hexcore.darwin.domain.permissions", "default_registry"),
    "reset_default_registry": (
        "hexcore.darwin.domain.permissions",
        "reset_default_registry",
    ),
    # ── Puertos ───────────────────────────────────────────────────────────────
    "AbstractClock": ("hexcore.darwin.domain.ports", "AbstractClock"),
    "AbstractPasswordHasher": ("hexcore.darwin.domain.ports", "AbstractPasswordHasher"),
    "AbstractUserRepository": ("hexcore.darwin.domain.ports", "AbstractUserRepository"),
    "AbstractSessionRepository": (
        "hexcore.darwin.domain.ports",
        "AbstractSessionRepository",
    ),
    "AbstractAccountRepository": (
        "hexcore.darwin.domain.ports",
        "AbstractAccountRepository",
    ),
    "AbstractVerificationRepository": (
        "hexcore.darwin.domain.ports",
        "AbstractVerificationRepository",
    ),
    "AbstractRevocationList": ("hexcore.darwin.domain.ports", "AbstractRevocationList"),
    "AbstractAuditSink": ("hexcore.darwin.domain.ports", "AbstractAuditSink"),
    # ── Excepciones ───────────────────────────────────────────────────────────
    "IdentityError": ("hexcore.darwin.domain.exceptions", "IdentityError"),
    "AuthenticationError": ("hexcore.darwin.domain.exceptions", "AuthenticationError"),
    "UnauthenticatedError": ("hexcore.darwin.domain.exceptions", "UnauthenticatedError"),
    "InvalidCredentialsError": (
        "hexcore.darwin.domain.exceptions",
        "InvalidCredentialsError",
    ),
    "TokenError": ("hexcore.darwin.domain.exceptions", "TokenError"),
    "TokenMalformedError": ("hexcore.darwin.domain.exceptions", "TokenMalformedError"),
    "TokenExpiredError": ("hexcore.darwin.domain.exceptions", "TokenExpiredError"),
    "TokenRevokedError": ("hexcore.darwin.domain.exceptions", "TokenRevokedError"),
    "TokenAudienceMismatchError": (
        "hexcore.darwin.domain.exceptions",
        "TokenAudienceMismatchError",
    ),
    "AuthorizationError": ("hexcore.darwin.domain.exceptions", "AuthorizationError"),
    "InsufficientScopeError": (
        "hexcore.darwin.domain.exceptions",
        "InsufficientScopeError",
    ),
    "EmailNotVerifiedError": (
        "hexcore.darwin.domain.exceptions",
        "EmailNotVerifiedError",
    ),
    "ImpersonationNotPermittedError": (
        "hexcore.darwin.domain.exceptions",
        "ImpersonationNotPermittedError",
    ),
    "CsrfValidationError": ("hexcore.darwin.domain.exceptions", "CsrfValidationError"),
    "EmailAlreadyRegisteredError": (
        "hexcore.darwin.domain.exceptions",
        "EmailAlreadyRegisteredError",
    ),
    "AccountLockedError": ("hexcore.darwin.domain.exceptions", "AccountLockedError"),
    "WorkerContextIntegrityError": (
        "hexcore.darwin.domain.exceptions",
        "WorkerContextIntegrityError",
    ),
    "IDENTITY_EXCEPTION_STATUS_MAP": (
        "hexcore.darwin.domain.exceptions",
        "IDENTITY_EXCEPTION_STATUS_MAP",
    ),
    # ── Eventos ───────────────────────────────────────────────────────────────
    "UserRegisteredEvent": ("hexcore.darwin.domain.events", "UserRegisteredEvent"),
    "UserEmailVerifiedEvent": (
        "hexcore.darwin.domain.events",
        "UserEmailVerifiedEvent",
    ),
    "UserPasswordChangedEvent": (
        "hexcore.darwin.domain.events",
        "UserPasswordChangedEvent",
    ),
    "UserSignedInEvent": ("hexcore.darwin.domain.events", "UserSignedInEvent"),
    "UserSignInFailedEvent": ("hexcore.darwin.domain.events", "UserSignInFailedEvent"),
    "SessionCreatedEvent": ("hexcore.darwin.domain.events", "SessionCreatedEvent"),
    "SessionRefreshedEvent": ("hexcore.darwin.domain.events", "SessionRefreshedEvent"),
    "SessionRevokedEvent": ("hexcore.darwin.domain.events", "SessionRevokedEvent"),
    "SessionReuseDetectedEvent": (
        "hexcore.darwin.domain.events",
        "SessionReuseDetectedEvent",
    ),
    "AllSessionsRevokedEvent": (
        "hexcore.darwin.domain.events",
        "AllSessionsRevokedEvent",
    ),
    "ImpersonationStartedEvent": (
        "hexcore.darwin.domain.events",
        "ImpersonationStartedEvent",
    ),
    "ImpersonationEndedEvent": (
        "hexcore.darwin.domain.events",
        "ImpersonationEndedEvent",
    ),
    "AccountLinkedEvent": ("hexcore.darwin.domain.events", "AccountLinkedEvent"),
    "AccountUnlinkedEvent": ("hexcore.darwin.domain.events", "AccountUnlinkedEvent"),
}

# `sorted(...)` no es una expresión que Pyright pueda evaluar, así que avisa que la
# lista de exports puede estar incompleta. Acá no lo está: el `__all__` **literal** que
# el checker usa de verdad vive en el `.pyi` generado, y `tests/test_typing_gate.py`
# verifica que coincida con éste. Se suprime la regla puntual, con motivo, en vez de
# duplicar 66 nombres a mano en el fuente.
__all__ = sorted(_EXPORTS)  # pyright: ignore[reportUnsupportedDunderAll]


def __getattr__(name: str) -> t.Any:
    try:
        module_path, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module 'hexcore.darwin' has no attribute {name!r}"
        ) from None

    value = getattr(importlib.import_module(module_path), attribute)
    # Se cachea en los globals: el segundo acceso no vuelve a pasar por acá.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return __all__
