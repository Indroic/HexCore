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
# los 146 símbolos de `hexcore.darwin` tipan `Any`. El runtime no cambia — Python usa
# el `.py` y el checker usa el `.pyi`, así que la carga perezosa se mantiene.


from hexcore.darwin._provisional import DarwinProvisionalWarning as DarwinProvisionalWarning
from hexcore.darwin.application.commands import AuthenticateToken as AuthenticateToken
from hexcore.darwin.application.commands import ChangePassword as ChangePassword
from hexcore.darwin.application.commands import IssueVerificationCode as IssueVerificationCode
from hexcore.darwin.application.commands import ListActiveSessions as ListActiveSessions
from hexcore.darwin.application.commands import RefreshResult as RefreshResult
from hexcore.darwin.application.commands import RefreshSession as RefreshSession
from hexcore.darwin.application.commands import SignIn as SignIn
from hexcore.darwin.application.commands import SignInResult as SignInResult
from hexcore.darwin.application.commands import SignOut as SignOut
from hexcore.darwin.application.commands import SignOutEverywhere as SignOutEverywhere
from hexcore.darwin.application.commands import SignUp as SignUp
from hexcore.darwin.application.commands import SignUpResult as SignUpResult
from hexcore.darwin.application.commands import VerifyEmail as VerifyEmail
from hexcore.darwin.application.commands import register_identity_handlers as register_identity_handlers
from hexcore.darwin.application.config import CookieConfig as CookieConfig
from hexcore.darwin.application.config import IdentityConfig as IdentityConfig
from hexcore.darwin.application.config import PasswordPolicy as PasswordPolicy
from hexcore.darwin.application.config import SECRET_KEY_ENV as SECRET_KEY_ENV
from hexcore.darwin.application.config import TokenConfig as TokenConfig
from hexcore.darwin.application.container import IdentityContainer as IdentityContainer
from hexcore.darwin.application.container import configure_identity as configure_identity
from hexcore.darwin.application.container import get_identity_container as get_identity_container
from hexcore.darwin.application.container import provide_identity as provide_identity
from hexcore.darwin.application.container import provide_identity_config as provide_identity_config
from hexcore.darwin.application.container import provide_session_service as provide_session_service
from hexcore.darwin.application.container import reset_identity as reset_identity
from hexcore.darwin.application.services import IdentityService as IdentityService
from hexcore.darwin.application.services import SessionService as SessionService
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
from hexcore.darwin.infrastructure.clock import FixedClock as FixedClock
from hexcore.darwin.infrastructure.clock import SystemClock as SystemClock
from hexcore.darwin.infrastructure.hashing import Argon2PasswordHasher as Argon2PasswordHasher
from hexcore.darwin.infrastructure.hashing import compare_hashes as compare_hashes
from hexcore.darwin.infrastructure.hashing import generate_numeric_code as generate_numeric_code
from hexcore.darwin.infrastructure.hashing import generate_token as generate_token
from hexcore.darwin.infrastructure.hashing import hash_token as hash_token
from hexcore.darwin.infrastructure.keys import AbstractKeyStore as AbstractKeyStore
from hexcore.darwin.infrastructure.keys import KeyStatus as KeyStatus
from hexcore.darwin.infrastructure.keys import NoActiveKeyError as NoActiveKeyError
from hexcore.darwin.infrastructure.keys import RetiredKeyError as RetiredKeyError
from hexcore.darwin.infrastructure.keys import SigningKey as SigningKey
from hexcore.darwin.infrastructure.keys import StaticKeyStore as StaticKeyStore
from hexcore.darwin.infrastructure.keys import UnknownKeyError as UnknownKeyError
from hexcore.darwin.infrastructure.keys import generate_signing_key as generate_signing_key
from hexcore.darwin.infrastructure.keys import jwks_document as jwks_document
from hexcore.darwin.infrastructure.models import AccountModel as AccountModel
from hexcore.darwin.infrastructure.models import AuditLogModel as AuditLogModel
from hexcore.darwin.infrastructure.models import IDENTITY_MODELS as IDENTITY_MODELS
from hexcore.darwin.infrastructure.models import JwksModel as JwksModel
from hexcore.darwin.infrastructure.models import SessionModel as SessionModel
from hexcore.darwin.infrastructure.models import UserModel as UserModel
from hexcore.darwin.infrastructure.models import VerificationModel as VerificationModel
from hexcore.darwin.infrastructure.models_mixins import AccountMixin as AccountMixin
from hexcore.darwin.infrastructure.models_mixins import AuditLogMixin as AuditLogMixin
from hexcore.darwin.infrastructure.models_mixins import DEFAULT_ACCOUNT_TABLE as DEFAULT_ACCOUNT_TABLE
from hexcore.darwin.infrastructure.models_mixins import DEFAULT_SESSION_TABLE as DEFAULT_SESSION_TABLE
from hexcore.darwin.infrastructure.models_mixins import DEFAULT_USER_TABLE as DEFAULT_USER_TABLE
from hexcore.darwin.infrastructure.models_mixins import DEFAULT_VERIFICATION_TABLE as DEFAULT_VERIFICATION_TABLE
from hexcore.darwin.infrastructure.models_mixins import JwksMixin as JwksMixin
from hexcore.darwin.infrastructure.models_mixins import SessionMixin as SessionMixin
from hexcore.darwin.infrastructure.models_mixins import TimestampMixin as TimestampMixin
from hexcore.darwin.infrastructure.models_mixins import UserMixin as UserMixin
from hexcore.darwin.infrastructure.models_mixins import VerificationMixin as VerificationMixin
from hexcore.darwin.infrastructure.repositories import SqlAlchemyAccountRepository as SqlAlchemyAccountRepository
from hexcore.darwin.infrastructure.repositories import SqlAlchemyAuditSink as SqlAlchemyAuditSink
from hexcore.darwin.infrastructure.repositories import SqlAlchemySessionRepository as SqlAlchemySessionRepository
from hexcore.darwin.infrastructure.repositories import SqlAlchemyUserRepository as SqlAlchemyUserRepository
from hexcore.darwin.infrastructure.repositories import SqlAlchemyVerificationRepository as SqlAlchemyVerificationRepository
from hexcore.darwin.infrastructure.revocation import CacheErrorPolicy as CacheErrorPolicy
from hexcore.darwin.infrastructure.revocation import CacheRevocationList as CacheRevocationList
from hexcore.darwin.infrastructure.revocation import GenerationGuard as GenerationGuard
from hexcore.darwin.infrastructure.schema import create_identity_tables as create_identity_tables
from hexcore.darwin.infrastructure.schema import drop_identity_tables as drop_identity_tables
from hexcore.darwin.infrastructure.schema import ensure_identity_schema_loaded as ensure_identity_schema_loaded
from hexcore.darwin.infrastructure.schema import identity_tables as identity_tables
from hexcore.darwin.infrastructure.schema import validate_user_model as validate_user_model
from hexcore.darwin.infrastructure.tokens import JoserfcTokenIssuer as JoserfcTokenIssuer
from hexcore.darwin.infrastructure.tokens import JoserfcTokenVerifier as JoserfcTokenVerifier
from hexcore.darwin.infrastructure.tokens import TokenTtl as TokenTtl
from hexcore.darwin.infrastructure.tokens import audience_for as audience_for

__all__ = [
    "AUTH_CONTEXT",
    "AbstractAccountRepository",
    "AbstractAuditSink",
    "AbstractClock",
    "AbstractKeyStore",
    "AbstractPasswordHasher",
    "AbstractRevocationList",
    "AbstractSessionRepository",
    "AbstractUserRepository",
    "AbstractVerificationRepository",
    "AccessTokenClaims",
    "Account",
    "AccountLinkedEvent",
    "AccountLockedError",
    "AccountMixin",
    "AccountModel",
    "AccountUnlinkedEvent",
    "AllSessionsRevokedEvent",
    "Argon2PasswordHasher",
    "AuditLogMixin",
    "AuditLogModel",
    "AuthContext",
    "AuthenticateToken",
    "AuthenticationError",
    "AuthorizationError",
    "CREDENTIAL_PROVIDER",
    "CacheErrorPolicy",
    "CacheRevocationList",
    "ChangePassword",
    "CookieConfig",
    "CsrfValidationError",
    "DEFAULT_ACCOUNT_TABLE",
    "DEFAULT_SESSION_TABLE",
    "DEFAULT_USER_TABLE",
    "DEFAULT_VERIFICATION_TABLE",
    "DarwinProvisionalWarning",
    "Email",
    "EmailAlreadyRegisteredError",
    "EmailNotVerifiedError",
    "FixedClock",
    "GenerationGuard",
    "IDENTITY_EXCEPTION_STATUS_MAP",
    "IDENTITY_MODELS",
    "IdentityConfig",
    "IdentityContainer",
    "IdentityError",
    "IdentityService",
    "IdentitySession",
    "Impersonation",
    "ImpersonationEndedEvent",
    "ImpersonationNotPermittedError",
    "ImpersonationStartedEvent",
    "InsufficientScopeError",
    "InvalidCredentialsError",
    "IssueVerificationCode",
    "JoserfcTokenIssuer",
    "JoserfcTokenVerifier",
    "JwksMixin",
    "JwksModel",
    "KeyStatus",
    "ListActiveSessions",
    "NoActiveKeyError",
    "PasswordPolicy",
    "Permission",
    "PermissionCycleError",
    "Principal",
    "RefreshResult",
    "RefreshSession",
    "RetiredKeyError",
    "Role",
    "RoleRegistry",
    "SECRET_KEY_ENV",
    "SessionCreatedEvent",
    "SessionMixin",
    "SessionModel",
    "SessionRefreshedEvent",
    "SessionReuseDetectedEvent",
    "SessionRevokedEvent",
    "SessionService",
    "SignIn",
    "SignInResult",
    "SignOut",
    "SignOutEverywhere",
    "SignUp",
    "SignUpResult",
    "SigningKey",
    "SqlAlchemyAccountRepository",
    "SqlAlchemyAuditSink",
    "SqlAlchemySessionRepository",
    "SqlAlchemyUserRepository",
    "SqlAlchemyVerificationRepository",
    "StaticKeyStore",
    "SystemClock",
    "SystemPrincipal",
    "TimestampMixin",
    "TokenAudienceMismatchError",
    "TokenConfig",
    "TokenError",
    "TokenExpiredError",
    "TokenMalformedError",
    "TokenPair",
    "TokenRevokedError",
    "TokenTtl",
    "TokenType",
    "Transport",
    "UnauthenticatedError",
    "UnknownKeyError",
    "User",
    "UserEmailVerifiedEvent",
    "UserMixin",
    "UserModel",
    "UserPasswordChangedEvent",
    "UserRegisteredEvent",
    "UserSignInFailedEvent",
    "UserSignedInEvent",
    "Verification",
    "VerificationMixin",
    "VerificationModel",
    "VerificationPurpose",
    "VerifyEmail",
    "WorkerContextIntegrityError",
    "audience_for",
    "auth_scope",
    "compare_hashes",
    "configure_identity",
    "create_identity_tables",
    "current_auth",
    "default_registry",
    "drop_identity_tables",
    "ensure_identity_schema_loaded",
    "generate_numeric_code",
    "generate_signing_key",
    "generate_token",
    "get_identity_container",
    "hash_token",
    "identity_tables",
    "jwks_document",
    "provide_identity",
    "provide_identity_config",
    "provide_session_service",
    "register_identity_handlers",
    "require_auth",
    "reset_default_registry",
    "reset_identity",
    "system_context",
    "validate_user_model",
]
