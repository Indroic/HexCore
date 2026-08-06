# ⚠️  ARCHIVO GENERADO — NO EDITAR A MANO.
#
# Generado por `scripts/gen_stubs.py` desde el `_EXPORTS` de `hexcore/darwin/__init__.py`.
# Si editás esto a mano, el job `stubs-drift` de CI te lo va a revertir.
#
# Para regenerar:
#
#     uv run python scripts/gen_stubs.py --write
#
# Existe porque la fachada resuelve sus exports con `__getattr__` y declara
# `__all__ = sorted(_EXPORTS)`: las dos son expresiones de runtime, así que sin este stub
# los 66 símbolos de `hexcore.darwin` tipan `Any`. El runtime no cambia — Python usa
# el `.py` y el checker usa el `.pyi`, así que la carga perezosa se mantiene.


from hexcore.darwin.domain.context import AUTH_CONTEXT as AUTH_CONTEXT
from hexcore.darwin.domain.context import AuthContext as AuthContext
from hexcore.darwin.domain.context import Impersonation as Impersonation
from hexcore.darwin.domain.context import Principal as Principal
from hexcore.darwin.domain.context import SystemPrincipal as SystemPrincipal
from hexcore.darwin.domain.context import Transport as Transport
from hexcore.darwin.domain.context import auth_scope as auth_scope
from hexcore.darwin.domain.context import current_auth as current_auth
from hexcore.darwin.domain.context import require_auth as require_auth
from hexcore.darwin.domain.context import system_context as system_context
from hexcore.darwin.domain.entities import Account as Account
from hexcore.darwin.domain.entities import CREDENTIAL_PROVIDER as CREDENTIAL_PROVIDER
from hexcore.darwin.domain.entities import IdentitySession as IdentitySession
from hexcore.darwin.domain.entities import User as User
from hexcore.darwin.domain.entities import Verification as Verification
from hexcore.darwin.domain.events import AccountLinkedEvent as AccountLinkedEvent
from hexcore.darwin.domain.events import AccountUnlinkedEvent as AccountUnlinkedEvent
from hexcore.darwin.domain.events import AllSessionsRevokedEvent as AllSessionsRevokedEvent
from hexcore.darwin.domain.events import ImpersonationEndedEvent as ImpersonationEndedEvent
from hexcore.darwin.domain.events import ImpersonationStartedEvent as ImpersonationStartedEvent
from hexcore.darwin.domain.events import SessionCreatedEvent as SessionCreatedEvent
from hexcore.darwin.domain.events import SessionRefreshedEvent as SessionRefreshedEvent
from hexcore.darwin.domain.events import SessionReuseDetectedEvent as SessionReuseDetectedEvent
from hexcore.darwin.domain.events import SessionRevokedEvent as SessionRevokedEvent
from hexcore.darwin.domain.events import UserEmailVerifiedEvent as UserEmailVerifiedEvent
from hexcore.darwin.domain.events import UserPasswordChangedEvent as UserPasswordChangedEvent
from hexcore.darwin.domain.events import UserRegisteredEvent as UserRegisteredEvent
from hexcore.darwin.domain.events import UserSignInFailedEvent as UserSignInFailedEvent
from hexcore.darwin.domain.events import UserSignedInEvent as UserSignedInEvent
from hexcore.darwin.domain.exceptions import AccountLockedError as AccountLockedError
from hexcore.darwin.domain.exceptions import AuthenticationError as AuthenticationError
from hexcore.darwin.domain.exceptions import AuthorizationError as AuthorizationError
from hexcore.darwin.domain.exceptions import CsrfValidationError as CsrfValidationError
from hexcore.darwin.domain.exceptions import EmailAlreadyRegisteredError as EmailAlreadyRegisteredError
from hexcore.darwin.domain.exceptions import EmailNotVerifiedError as EmailNotVerifiedError
from hexcore.darwin.domain.exceptions import IDENTITY_EXCEPTION_STATUS_MAP as IDENTITY_EXCEPTION_STATUS_MAP
from hexcore.darwin.domain.exceptions import IdentityError as IdentityError
from hexcore.darwin.domain.exceptions import ImpersonationNotPermittedError as ImpersonationNotPermittedError
from hexcore.darwin.domain.exceptions import InsufficientScopeError as InsufficientScopeError
from hexcore.darwin.domain.exceptions import InvalidCredentialsError as InvalidCredentialsError
from hexcore.darwin.domain.exceptions import TokenAudienceMismatchError as TokenAudienceMismatchError
from hexcore.darwin.domain.exceptions import TokenError as TokenError
from hexcore.darwin.domain.exceptions import TokenExpiredError as TokenExpiredError
from hexcore.darwin.domain.exceptions import TokenMalformedError as TokenMalformedError
from hexcore.darwin.domain.exceptions import TokenRevokedError as TokenRevokedError
from hexcore.darwin.domain.exceptions import UnauthenticatedError as UnauthenticatedError
from hexcore.darwin.domain.exceptions import WorkerContextIntegrityError as WorkerContextIntegrityError
from hexcore.darwin.domain.permissions import Permission as Permission
from hexcore.darwin.domain.permissions import PermissionCycleError as PermissionCycleError
from hexcore.darwin.domain.permissions import Role as Role
from hexcore.darwin.domain.permissions import RoleRegistry as RoleRegistry
from hexcore.darwin.domain.permissions import default_registry as default_registry
from hexcore.darwin.domain.permissions import reset_default_registry as reset_default_registry
from hexcore.darwin.domain.ports import AbstractAccountRepository as AbstractAccountRepository
from hexcore.darwin.domain.ports import AbstractAuditSink as AbstractAuditSink
from hexcore.darwin.domain.ports import AbstractClock as AbstractClock
from hexcore.darwin.domain.ports import AbstractPasswordHasher as AbstractPasswordHasher
from hexcore.darwin.domain.ports import AbstractRevocationList as AbstractRevocationList
from hexcore.darwin.domain.ports import AbstractSessionRepository as AbstractSessionRepository
from hexcore.darwin.domain.ports import AbstractUserRepository as AbstractUserRepository
from hexcore.darwin.domain.ports import AbstractVerificationRepository as AbstractVerificationRepository
from hexcore.darwin.domain.value_objects import AccessTokenClaims as AccessTokenClaims
from hexcore.darwin.domain.value_objects import Email as Email
from hexcore.darwin.domain.value_objects import TokenPair as TokenPair
from hexcore.darwin.domain.value_objects import TokenType as TokenType
from hexcore.darwin.domain.value_objects import VerificationPurpose as VerificationPurpose

__all__ = [
    "AUTH_CONTEXT",
    "AbstractAccountRepository",
    "AbstractAuditSink",
    "AbstractClock",
    "AbstractPasswordHasher",
    "AbstractRevocationList",
    "AbstractSessionRepository",
    "AbstractUserRepository",
    "AbstractVerificationRepository",
    "AccessTokenClaims",
    "Account",
    "AccountLinkedEvent",
    "AccountLockedError",
    "AccountUnlinkedEvent",
    "AllSessionsRevokedEvent",
    "AuthContext",
    "AuthenticationError",
    "AuthorizationError",
    "CREDENTIAL_PROVIDER",
    "CsrfValidationError",
    "Email",
    "EmailAlreadyRegisteredError",
    "EmailNotVerifiedError",
    "IDENTITY_EXCEPTION_STATUS_MAP",
    "IdentityError",
    "IdentitySession",
    "Impersonation",
    "ImpersonationEndedEvent",
    "ImpersonationNotPermittedError",
    "ImpersonationStartedEvent",
    "InsufficientScopeError",
    "InvalidCredentialsError",
    "Permission",
    "PermissionCycleError",
    "Principal",
    "Role",
    "RoleRegistry",
    "SessionCreatedEvent",
    "SessionRefreshedEvent",
    "SessionReuseDetectedEvent",
    "SessionRevokedEvent",
    "SystemPrincipal",
    "TokenAudienceMismatchError",
    "TokenError",
    "TokenExpiredError",
    "TokenMalformedError",
    "TokenPair",
    "TokenRevokedError",
    "TokenType",
    "Transport",
    "UnauthenticatedError",
    "User",
    "UserEmailVerifiedEvent",
    "UserPasswordChangedEvent",
    "UserRegisteredEvent",
    "UserSignInFailedEvent",
    "UserSignedInEvent",
    "Verification",
    "VerificationPurpose",
    "WorkerContextIntegrityError",
    "auth_scope",
    "current_auth",
    "default_registry",
    "require_auth",
    "reset_default_registry",
    "system_context",
]
