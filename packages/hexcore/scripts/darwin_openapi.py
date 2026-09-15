"""
Vuelca el contrato HTTP de Darwin a `packages/darwin-client/openapi/`.

Es la fuente de verdad de la que el cliente TypeScript va a generar tipos (Fase 2). El
artefacto se versiona en git y no sólo se genera en CI: así el drift aparece en el **diff del
PR**, donde alguien lo lee, en vez de en un log que se mira sólo cuando está rojo.

Por qué no `app.openapi_url` en producción: `openapi_url=None` apaga la **ruta HTTP**, pero
`app.openapi()` en Python sigue devolviendo el documento completo — encender esa ruta por
defecto prendería `/openapi.json` en todo despliegue existente, que es un cambio de superficie
de seguridad, no de DX.

Los `securitySchemes` se inyectan acá y no en `create_app`: meter `HTTPBearer`/`APIKeyCookie`
como dependencias reales sería peor. `APIKeyCookie` necesita el nombre de la cookie en tiempo de
import, y el nombre real depende de `CookieConfig.secure` en runtime — documentaría un nombre
que a veces es mentira. Las rutas protegidas se detectan recorriendo `route.dependant`
aplanado en busca de `provide_auth`, no con una lista a mano: una lista se desincroniza siempre,
que es el mismo argumento del docstring de `AuthContextMiddleware` sobre las rutas públicas.

Uso::

    uv run python scripts/darwin_openapi.py --write   # regenera
    uv run python scripts/darwin_openapi.py --check   # falla si hay drift (lo que corre CI)
"""
from __future__ import annotations

import argparse
import json
import sys
import typing as t
from importlib.metadata import version as _pkg_version
from pathlib import Path

from _rutas import PAQUETE

OUT_DIR = PAQUETE.parent / "darwin-client" / "openapi"

#: Los dos esquemas de auth que Darwin acepta. `darwinBearer` documenta el header
#: `Authorization: Bearer …`; `darwinCookie` documenta la cookie de acceso. Los dos nombres de
#: cookie reales (`__Host-` o no, según `CookieConfig.secure`) son un detalle de despliegue que
#: no pertenece a un contrato versionado en git.
SECURITY_SCHEMES: dict[str, dict[str, str]] = {
    "darwinBearer": {"type": "http", "scheme": "bearer"},
    "darwinCookie": {"type": "apiKey", "in": "cookie", "name": "access_token"},
}


def _construir_app() -> t.Any:
    """
    Monta una app con los siete routers y los seis plugins, sin base ni contenedor real.

    `configure_test_identity` alcanza: ningún `build_*_router()` toca
    `get_identity_container()` al **construir** — todos los `from … import get_X_service`
    están *dentro* de los handlers — así que levantar la app no necesita SQLAlchemy ni Mongo,
    sólo el extra `[api]`.
    """
    from hexcore.darwin import PluginRegistry, build_identity_router
    from hexcore.darwin.plugins.impersonate import ImpersonatePlugin
    from hexcore.darwin.plugins.magic_link import MagicLinkPlugin
    from hexcore.darwin.plugins.oauth import OAuthPlugin
    from hexcore.darwin.plugins.organization import OrganizationPlugin
    from hexcore.darwin.plugins.passkey import PasskeyPlugin
    from hexcore.darwin.plugins.two_factor import TwoFactorPlugin
    from hexcore.darwin.testing import configure_test_identity
    from hexcore.fastapi import AppFeatures, create_app

    plugins = PluginRegistry(
        [
            TwoFactorPlugin(),
            MagicLinkPlugin(),
            OAuthPlugin(providers=()),
            PasskeyPlugin(rp_id="darwin-openapi.local", origins=["https://darwin-openapi.local"]),
            ImpersonatePlugin(),
            OrganizationPlugin(),
        ]
    )
    configure_test_identity(plugins=plugins)

    return create_app(
        title="Darwin",
        features=AppFeatures(auth_context=True, csrf=True, health=False),
        routers=[build_identity_router(), *plugins.routers()],
    )


def _es_ruta_protegida(route: t.Any) -> bool:
    """
    Si `provide_auth` aparece en el árbol de dependencias de la ruta, aplanado.

    `Dependant.dependencies` es recursivo (una dependencia puede depender de otra), así que se
    recorre en profundidad. Comparar por `__name__` y no por identidad de objeto: FastAPI envuelve
    cada `Depends(...)` en su propio `Dependant`, y lo único estable entre esos wrappers es el
    callable original y su nombre.
    """
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False

    pila = [dependant]
    vistos: set[int] = set()
    while pila:
        actual = pila.pop()
        if id(actual) in vistos:
            continue
        vistos.add(id(actual))

        call = getattr(actual, "call", None)
        if call is not None and getattr(call, "__name__", None) == "provide_auth":
            return True

        pila.extend(getattr(actual, "dependencies", None) or [])

    return False


def _inyectar_seguridad(app: t.Any, schema: dict[str, t.Any]) -> None:
    schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES

    rutas_protegidas = {
        (route.path, metodo.lower())
        for route in app.routes
        if hasattr(route, "dependant") and _es_ruta_protegida(route)
        for metodo in getattr(route, "methods", ()) or ()
    }

    for ruta, operaciones in schema.get("paths", {}).items():
        for metodo, operacion in operaciones.items():
            if (ruta, metodo) in rutas_protegidas:
                operacion["security"] = [{"darwinBearer": []}, {"darwinCookie": []}]


def _errores_combinados() -> dict[str, int]:
    """`{NombreDeLaExcepción: status}`, del núcleo más los seis plugins."""
    from hexcore.darwin.application.container import get_identity_container
    from hexcore.darwin.domain.exceptions import IDENTITY_EXCEPTION_STATUS_MAP

    contenedor = get_identity_container()
    combinado = {**IDENTITY_EXCEPTION_STATUS_MAP, **contenedor.plugins.exception_status_map()}
    return {exc.__name__: status for exc, status in combinado.items()}


def _escribir(path: Path, contenido: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contenido, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--write", action="store_true", help="Regenera los tres archivos.")
    grupo.add_argument(
        "--check", action="store_true", help="Falla si el árbol difiere de lo regenerado."
    )
    args = parser.parse_args()

    app = _construir_app()
    schema = app.openapi()
    _inyectar_seguridad(app, schema)

    openapi_json = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    errors_json = (
        json.dumps(_errores_combinados(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    version_txt = _pkg_version("hexcore") + "\n"

    objetivos = {
        OUT_DIR / "darwin.openapi.json": openapi_json,
        OUT_DIR / "darwin.errors.json": errors_json,
        OUT_DIR / "VERSION": version_txt,
    }

    if args.write:
        for ruta, contenido in objetivos.items():
            _escribir(ruta, contenido)
            print(f"escrito {ruta}")
        return 0

    # --check
    faltantes_o_distintos: list[str] = []
    for ruta, esperado in objetivos.items():
        actual = ruta.read_text(encoding="utf-8") if ruta.is_file() else None
        if actual != esperado:
            faltantes_o_distintos.append(str(ruta))

    if faltantes_o_distintos:
        print("::error::El contrato de Darwin quedó desactualizado en:")
        for ruta in faltantes_o_distintos:
            print(f"  - {ruta}")
        print("\nRegeneralo con: uv run python scripts/darwin_openapi.py --write")
        return 1

    print("Contrato de Darwin: en verde (sin drift).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
