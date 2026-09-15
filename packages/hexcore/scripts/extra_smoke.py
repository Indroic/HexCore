"""
Verifica que un extra instalado **solo** alcance para lo que promete.

El punto ciego que esto cierra: CI instalaba `--extra all` y nada más. Con todo instalado, un
módulo que importa `sqlalchemy` sin guarda anda igual, y un extra que se olvidó de declarar una
dependencia también. Los dos se rompen recién en la máquina del consumidor, que instaló uno solo.

Cada pata de la matriz corre esto con un único extra puesto, y se comprueban dos cosas opuestas:

1. **Las cuatro fachadas importan siempre**, con cero extras incluido. Resuelven perezosamente,
   así que nombrarlas no puede arrastrar nada.
2. **El símbolo que el extra promete resuelve**, y para eso hace falta que el extra traiga de
   verdad lo que dice. `hexcore[darwin-two-factor]` tiene que dar un `TwoFactorPlugin` usable:
   sin la autorreferencia a `hexcore[darwin]` en el `pyproject`, ese comando instalaba un plugin
   sin núcleo y el import se rompía.

Uso::

    uv run python scripts/extra_smoke.py none
    uv run python scripts/extra_smoke.py darwin-two-factor
"""
from __future__ import annotations

import importlib
import sys
import typing as t

#: Las fachadas perezosas. Importarlas tiene que andar con cero extras.
FACHADAS: tuple[str, ...] = (
    "hexcore",
    "hexcore.cqrs",
    "hexcore.sql",
    "hexcore.fastapi",
    "hexcore.darwin",
)

#: Qué símbolo prueba que el extra sirve, por `(módulo, atributo)`.
#:
#: Se pide un símbolo **que ejercite la dependencia**, no cualquiera: `hexcore.sql.Base` toca
#: SQLAlchemy de verdad, mientras que un nombre que resuelva sin importar nada no probaría nada.
#: `none` no tiene fila: su prueba es que las fachadas importen, y eso ya corre siempre.
PROMESAS: dict[str, tuple[tuple[str, str], ...]] = {
    "api": (("hexcore.fastapi", "create_app"),),
    "sql": (("hexcore.sql", "Base"), ("hexcore.sql", "init_engine")),
    "mongo": (
        ("hexcore.infrastructure.repositories.orms.beanie", "BaseDocument"),
        ("hexcore.infrastructure.repositories.implementations", "BeanieRepository"),
    ),
    "redis": (("hexcore.cqrs", "RedisLockProvider"),),
    "rabbitmq": (("hexcore.infrastructure.cqrs.rabbitmq", "RabbitMQEventBus"),),
    "procrastinate": (
        ("hexcore.cqrs", "run_procrastinate_worker"),
        ("hexcore.infrastructure.task_queues.procrastinate_adapter", "ProcrastinateEnqueuer"),
    ),
    "celery": (("hexcore.infrastructure.task_queues.celery_adapter", "CeleryEnqueuer"),),
    # ── Darwin ────────────────────────────────────────────────────────────────
    "darwin": (
        ("hexcore.darwin", "IdentityConfig"),
        ("hexcore.darwin", "JoserfcTokenIssuer"),
        ("hexcore.darwin", "configure_identity"),
    ),
    "darwin-sqlalchemy": (
        ("hexcore.darwin", "ensure_identity_schema_loaded"),
        ("hexcore.darwin", "UserModel"),
    ),
    "darwin-beanie": (
        ("hexcore.darwin.infrastructure.orms.beanie.schema", "identity_documents"),
    ),
    "darwin-magic-link": (("hexcore.darwin.plugins.magic_link", "MagicLinkPlugin"),),
    "darwin-two-factor": (("hexcore.darwin.plugins.two_factor", "TwoFactorPlugin"),),
    "darwin-oauth": (("hexcore.darwin.plugins.oauth", "OAuthPlugin"),),
    "darwin-impersonate": (("hexcore.darwin.plugins.impersonate", "ImpersonatePlugin"),),
    "darwin-passkey": (("hexcore.darwin.plugins.passkey", "PasskeyPlugin"),),
    "darwin-organization": (
        ("hexcore.darwin.plugins.organization", "OrganizationPlugin"),
    ),
}


def _resolver(modulo: str, atributo: str) -> t.Any:
    return getattr(importlib.import_module(modulo), atributo)


def main(extra: str) -> int:
    fallos: list[str] = []

    for fachada in FACHADAS:
        try:
            importlib.import_module(fachada)
        except Exception as exc:  # noqa: BLE001
            fallos.append(
                f"`import {fachada}` falló con el extra [{extra}]: {type(exc).__name__}: {exc}\n"
                f"    Las fachadas resuelven perezosamente: importarlas no puede exigir ningún "
                f"extra."
            )

    if extra == "all":
        promesas = tuple(p for grupo in PROMESAS.values() for p in grupo)
    else:
        promesas = PROMESAS.get(extra, ())

    for modulo, atributo in promesas:
        try:
            _resolver(modulo, atributo)
        except Exception as exc:  # noqa: BLE001
            fallos.append(
                f"`{modulo}.{atributo}` no resuelve con el extra [{extra}] instalado: "
                f"{type(exc).__name__}: {exc}\n"
                f"    O al extra le falta una dependencia en `pyproject.toml`, o el módulo "
                f"importa algo que ese extra no trae."
            )

    if fallos:
        for f in fallos:
            print(f"::error::{f}")
        print()
        print(f"El extra [{extra}] no cumple lo que promete: {len(fallos)} fallo(s).")
        return 1

    revisadas = len(FACHADAS) + len(promesas)
    print(f"Extra [{extra}]: en verde ({revisadas} comprobación(es)).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python scripts/extra_smoke.py <extra|none|all>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
