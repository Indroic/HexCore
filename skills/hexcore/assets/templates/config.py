"""
config.py for a HexCore project, at the repository root.

`LazyConfig` looks for this module last, after HEXCORE_CONFIG_MODULE,
HEXCORE_CONFIG_MODULES and LazyConfig.set_config_modules([...]). There is no I/O and no
configuration resolution at import time, so you can redirect it before anything reads it.

You may export an instance named `config`, a class named `config` deriving from ServerConfig,
or a class named `ServerConfig` deriving from the base. All three are accepted.
"""
from __future__ import annotations

from pathlib import Path

from hexcore.config import ServerConfig

config = ServerConfig(
    # ---- Project -----------------------------------------------------------
    base_dir=Path(__file__).resolve().parent,
    app_title="My API",
    app_version="0.1.0",
    debug=False,
    host="0.0.0.0",
    port=8000,
    # ---- Databases ---------------------------------------------------------
    # Both are needed and it is not redundancy: Alembic runs migrations synchronously and
    # the app runs asynchronously. `init_engine()` reads the async one; env.py reads the
    # sync one.
    sql_database_url="postgresql://user:pass@localhost/mydb",
    async_sql_database_url="postgresql+asyncpg://user:pass@localhost/mydb",
    # ---- Repository discovery ----------------------------------------------
    # REQUIRED. The Unit of Work instantiates repositories from these modules and exposes
    # them as attributes (uow.tickets, uow.users). With an empty set it FAILS TO BUILD, with
    # a diagnostic error -- deliberately, rather than guessing paths by folder convention,
    # which tied the framework to one layout and failed silently on any other.
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
        "myapp.features.tickets.infrastructure.repositories",
    },
    # ---- CORS --------------------------------------------------------------
    # Declared explicitly because this project uses session cookies.
    #
    # "*" together with allow_credentials=True is NEVER valid: the browser cannot receive
    # "*" with credentials, so Starlette reflects the attacker's Origin and adds
    # Access-Control-Allow-Credentials: true -- any origin then reads authenticated
    # responses with the victim's cookie, no XSS required. Declaring both stops the app from
    # starting; declaring only "*" silently lowers allow_credentials to False, at which
    # point cookie auth quietly stops working.
    allow_origins=["https://app.example.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Optional modules --------------------------------------------------------
#
# Uncomment what the project actually uses. Each is a sibling field on ServerConfig, and
# each is typed `t.Any` on purpose: annotating them properly would force hexcore.config to
# import those modules, which loads half the framework -- including the CLI.
#
# CQRS:
#     from hexcore.application.cqrs.config import BusConfig, CQRSConfig
#     config.cqrs = CQRSConfig(
#         command_bus=BusConfig(
#             middlewares=["hexcore.infrastructure.cqrs.middlewares.LoggingMiddleware"],
#         ),
#     )
#
# Identity. Note the signing key is NOT here: every ServerConfig field has a default, and a
# signing secret with a default is the worst thing an auth library can ship. It lives in
# IdentityConfig.secret_key as a SecretStr with no default, read from
# HEXCORE_DARWIN_SECRET_KEY. Generate one with `hexcore identity generate-secret`.
#     from hexcore.darwin import IdentityConfig
#     config.darwin = IdentityConfig()
#
# Event store:
#     from hexcore.eventsourcing import EventStoreConfig
#     config.event_store = EventStoreConfig(
#         backend="hexcore.eventsourcing.SqlAlchemyEventStore",
#     )
#
# Production infrastructure -- swap the in-memory defaults:
#     from hexcore.infrastructure.cache.cache_backends.redis import RedisCache
#     config.cache_backend = RedisCache(...)
