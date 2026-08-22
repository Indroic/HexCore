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

Darwin dejó de ser API provisional en la Fase 7: el borde HTTP cerró las formas de
`AuthContext`, de los puertos, de los transportes y del emisor de tokens. Las fases que
siguen —plugins y kit de testing— **agregan** superficie sin cambiar la que ya está.

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
    # ── Persistencia (requiere el extra [darwin]) ─────────────────────────────
    "UserMixin": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins", "UserMixin"),
    "SessionMixin": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins", "SessionMixin"),
    "AccountMixin": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins", "AccountMixin"),
    "VerificationMixin": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins",
        "VerificationMixin",
    ),
    "AuditLogMixin": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins", "AuditLogMixin"),
    "JwksMixin": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins", "JwksMixin"),
    "TimestampMixin": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins", "TimestampMixin"),
    "DEFAULT_USER_TABLE": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins",
        "DEFAULT_USER_TABLE",
    ),
    "DEFAULT_SESSION_TABLE": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins",
        "DEFAULT_SESSION_TABLE",
    ),
    "DEFAULT_ACCOUNT_TABLE": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins",
        "DEFAULT_ACCOUNT_TABLE",
    ),
    "DEFAULT_VERIFICATION_TABLE": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins",
        "DEFAULT_VERIFICATION_TABLE",
    ),
    "UserModel": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models", "UserModel"),
    "SessionModel": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models", "SessionModel"),
    "AccountModel": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models", "AccountModel"),
    "VerificationModel": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models", "VerificationModel"),
    "AuditLogModel": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models", "AuditLogModel"),
    "JwksModel": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models", "JwksModel"),
    "IDENTITY_MODELS": ("hexcore.darwin.infrastructure.orms.sqlalchemy.models", "IDENTITY_MODELS"),
    "create_identity_tables": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.schema",
        "create_identity_tables",
    ),
    "drop_identity_tables": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.schema",
        "drop_identity_tables",
    ),
    "identity_tables": ("hexcore.darwin.infrastructure.orms.sqlalchemy.schema", "identity_tables"),
    "validate_user_model": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.schema",
        "validate_user_model",
    ),
    "ensure_identity_schema_loaded": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.schema",
        "ensure_identity_schema_loaded",
    ),
    "SqlAlchemyUserRepository": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.repositories",
        "SqlAlchemyUserRepository",
    ),
    "SqlAlchemySessionRepository": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.repositories",
        "SqlAlchemySessionRepository",
    ),
    "SqlAlchemyAccountRepository": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.repositories",
        "SqlAlchemyAccountRepository",
    ),
    "SqlAlchemyVerificationRepository": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.repositories",
        "SqlAlchemyVerificationRepository",
    ),
    "SqlAlchemyAuditSink": (
        "hexcore.darwin.infrastructure.orms.sqlalchemy.repositories",
        "SqlAlchemyAuditSink",
    ),
    # ── Crypto (requiere el extra [darwin]) ───────────────────────────────────
    "SystemClock": ("hexcore.darwin.infrastructure.clock", "SystemClock"),
    "FixedClock": ("hexcore.darwin.infrastructure.clock", "FixedClock"),
    "Argon2PasswordHasher": (
        "hexcore.darwin.infrastructure.hashing",
        "Argon2PasswordHasher",
    ),
    "hash_token": ("hexcore.darwin.infrastructure.hashing", "hash_token"),
    "compare_hashes": ("hexcore.darwin.infrastructure.hashing", "compare_hashes"),
    "generate_token": ("hexcore.darwin.infrastructure.hashing", "generate_token"),
    "generate_numeric_code": (
        "hexcore.darwin.infrastructure.hashing",
        "generate_numeric_code",
    ),
    "SigningKey": ("hexcore.darwin.infrastructure.keys", "SigningKey"),
    "KeyStatus": ("hexcore.darwin.infrastructure.keys", "KeyStatus"),
    "AbstractKeyStore": ("hexcore.darwin.infrastructure.keys", "AbstractKeyStore"),
    "StaticKeyStore": ("hexcore.darwin.infrastructure.keys", "StaticKeyStore"),
    "generate_signing_key": (
        "hexcore.darwin.infrastructure.keys",
        "generate_signing_key",
    ),
    "jwks_document": ("hexcore.darwin.infrastructure.keys", "jwks_document"),
    "UnknownKeyError": ("hexcore.darwin.infrastructure.keys", "UnknownKeyError"),
    "RetiredKeyError": ("hexcore.darwin.infrastructure.keys", "RetiredKeyError"),
    "NoActiveKeyError": ("hexcore.darwin.infrastructure.keys", "NoActiveKeyError"),
    "TokenTtl": ("hexcore.darwin.infrastructure.tokens", "TokenTtl"),
    "JoserfcTokenIssuer": (
        "hexcore.darwin.infrastructure.tokens",
        "JoserfcTokenIssuer",
    ),
    "JoserfcTokenVerifier": (
        "hexcore.darwin.infrastructure.tokens",
        "JoserfcTokenVerifier",
    ),
    "audience_for": ("hexcore.darwin.infrastructure.tokens", "audience_for"),
    "CacheRevocationList": (
        "hexcore.darwin.infrastructure.revocation",
        "CacheRevocationList",
    ),
    "GenerationGuard": (
        "hexcore.darwin.infrastructure.revocation",
        "GenerationGuard",
    ),
    "CacheErrorPolicy": (
        "hexcore.darwin.infrastructure.revocation",
        "CacheErrorPolicy",
    ),
    # ── El sobre que cruza la cola (Fase 6) ─────────────────────────────────
    "ENVELOPE_KEY": ("hexcore.darwin.infrastructure.envelope", "ENVELOPE_KEY"),
    "ENVELOPE_VERSION": ("hexcore.darwin.infrastructure.envelope", "ENVELOPE_VERSION"),
    "AuthEnvelopeCodec": (
        "hexcore.darwin.infrastructure.envelope",
        "AuthEnvelopeCodec",
    ),
    "AuthEnvelopeRestorer": (
        "hexcore.darwin.infrastructure.envelope",
        "AuthEnvelopeRestorer",
    ),
    "auth_envelope_provider": (
        "hexcore.darwin.infrastructure.envelope",
        "auth_envelope_provider",
    ),
    # ── Sistema de plugins (Fase 8) ──────────────────────────────────────────
    "DarwinPlugin": ("hexcore.darwin.domain.plugins", "DarwinPlugin"),
    "HookBinding": ("hexcore.darwin.domain.plugins", "HookBinding"),
    "HookPhase": ("hexcore.darwin.domain.plugins", "HookPhase"),
    "ShortCircuit": ("hexcore.darwin.domain.plugins", "ShortCircuit"),
    "identity_action": ("hexcore.darwin.domain.plugins", "identity_action"),
    "action_of": ("hexcore.darwin.domain.plugins", "action_of"),
    "PluginRegistry": ("hexcore.darwin.application.plugins", "PluginRegistry"),
    "PluginError": ("hexcore.darwin.application.plugins", "PluginError"),
    "HookMiddleware": ("hexcore.darwin.application.hooks", "HookMiddleware"),
    "run_hooks": ("hexcore.darwin.application.hooks", "run_hooks"),
    "SIGN_IN_AUTHENTICATED": (
        "hexcore.darwin.application.services",
        "SIGN_IN_AUTHENTICATED",
    ),
    # ── Borde HTTP (Fase 7 — requiere el extra [api]) ────────────────────────
    "TRANSPORT_HEADER": ("hexcore.darwin.infrastructure.transports", "TRANSPORT_HEADER"),
    "AbstractTransport": (
        "hexcore.darwin.infrastructure.transports",
        "AbstractTransport",
    ),
    "CookieTransport": ("hexcore.darwin.infrastructure.transports", "CookieTransport"),
    "BearerTransport": ("hexcore.darwin.infrastructure.transports", "BearerTransport"),
    "TransportResolver": (
        "hexcore.darwin.infrastructure.transports",
        "TransportResolver",
    ),
    "CSRF_HEADER": ("hexcore.darwin.infrastructure.api.middlewares", "CSRF_HEADER"),
    "AuthContextMiddleware": (
        "hexcore.darwin.infrastructure.api.middlewares",
        "AuthContextMiddleware",
    ),
    "CsrfMiddleware": (
        "hexcore.darwin.infrastructure.api.middlewares",
        "CsrfMiddleware",
    ),
    "auth_from_request": (
        "hexcore.darwin.infrastructure.api.middlewares",
        "auth_from_request",
    ),
    "WWW_AUTHENTICATE": (
        "hexcore.darwin.infrastructure.api.dependencies",
        "WWW_AUTHENTICATE",
    ),
    "provide_auth": (
        "hexcore.darwin.infrastructure.api.dependencies",
        "provide_auth",
    ),
    "provide_optional_auth": (
        "hexcore.darwin.infrastructure.api.dependencies",
        "provide_optional_auth",
    ),
    "require_authenticated": (
        "hexcore.darwin.infrastructure.api.dependencies",
        "require_authenticated",
    ),
    "require_scopes": (
        "hexcore.darwin.infrastructure.api.dependencies",
        "require_scopes",
    ),
    "require_roles": (
        "hexcore.darwin.infrastructure.api.dependencies",
        "require_roles",
    ),
    "require_not_impersonated": (
        "hexcore.darwin.infrastructure.api.dependencies",
        "require_not_impersonated",
    ),
    "identity_exception_headers": (
        "hexcore.darwin.infrastructure.api.dependencies",
        "identity_exception_headers",
    ),
    "build_identity_router": (
        "hexcore.darwin.infrastructure.api.routers",
        "build_identity_router",
    ),
    "emit_tokens": ("hexcore.darwin.infrastructure.api.routers", "emit_tokens"),
    "session_response_body": (
        "hexcore.darwin.infrastructure.api.routers",
        "session_response_body",
    ),
    "resolve_transport": (
        "hexcore.darwin.infrastructure.api.routers",
        "resolve_transport",
    ),
    "SessionResponse": ("hexcore.darwin.infrastructure.api.routers", "SessionResponse"),
    "MeResponse": ("hexcore.darwin.infrastructure.api.routers", "MeResponse"),
    "SignInRequest": ("hexcore.darwin.infrastructure.api.routers", "SignInRequest"),
    "SignUpRequest": ("hexcore.darwin.infrastructure.api.routers", "SignUpRequest"),
    "VerifyEmailRequest": (
        "hexcore.darwin.infrastructure.api.routers",
        "VerifyEmailRequest",
    ),
    "IdentityStep": ("hexcore.darwin.infrastructure.lifespan", "IdentityStep"),
    "SessionReaperStep": (
        "hexcore.darwin.infrastructure.lifespan",
        "SessionReaperStep",
    ),
    "identity_startup_steps": (
        "hexcore.darwin.infrastructure.lifespan",
        "identity_startup_steps",
    ),
    "derive_csrf_token": (
        "hexcore.darwin.infrastructure.hashing",
        "derive_csrf_token",
    ),
    # ── Capa de aplicación ────────────────────────────────────────────────────
    "IdentityConfig": ("hexcore.darwin.application.config", "IdentityConfig"),
    "TokenConfig": ("hexcore.darwin.application.config", "TokenConfig"),
    "CookieConfig": ("hexcore.darwin.application.config", "CookieConfig"),
    "PasswordPolicy": ("hexcore.darwin.application.config", "PasswordPolicy"),
    "SECRET_KEY_ENV": ("hexcore.darwin.application.config", "SECRET_KEY_ENV"),
    "IdentityContainer": (
        "hexcore.darwin.application.container",
        "IdentityContainer",
    ),
    "configure_identity": (
        "hexcore.darwin.application.container",
        "configure_identity",
    ),
    "get_identity_container": (
        "hexcore.darwin.application.container",
        "get_identity_container",
    ),
    "reset_identity": ("hexcore.darwin.application.container", "reset_identity"),
    "provide_identity": ("hexcore.darwin.application.container", "provide_identity"),
    "provide_session_service": (
        "hexcore.darwin.application.container",
        "provide_session_service",
    ),
    "provide_identity_config": (
        "hexcore.darwin.application.container",
        "provide_identity_config",
    ),
    "IdentityService": ("hexcore.darwin.application.services", "IdentityService"),
    "SessionService": ("hexcore.darwin.application.services", "SessionService"),
    # ── Comandos, queries y handlers ──────────────────────────────────────────
    "SignUp": ("hexcore.darwin.application.commands", "SignUp"),
    "VerifyEmail": ("hexcore.darwin.application.commands", "VerifyEmail"),
    "SignIn": ("hexcore.darwin.application.commands", "SignIn"),
    "RefreshSession": ("hexcore.darwin.application.commands", "RefreshSession"),
    "SignOut": ("hexcore.darwin.application.commands", "SignOut"),
    "SignOutEverywhere": (
        "hexcore.darwin.application.commands",
        "SignOutEverywhere",
    ),
    "ChangePassword": ("hexcore.darwin.application.commands", "ChangePassword"),
    "IssueVerificationCode": (
        "hexcore.darwin.application.commands",
        "IssueVerificationCode",
    ),
    "AuthenticateToken": (
        "hexcore.darwin.application.commands",
        "AuthenticateToken",
    ),
    "ListActiveSessions": (
        "hexcore.darwin.application.commands",
        "ListActiveSessions",
    ),
    "SignUpResult": ("hexcore.darwin.application.commands", "SignUpResult"),
    "SignInResult": ("hexcore.darwin.application.commands", "SignInResult"),
    "RefreshResult": ("hexcore.darwin.application.commands", "RefreshResult"),
    "register_identity_handlers": (
        "hexcore.darwin.application.commands",
        "register_identity_handlers",
    ),
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
