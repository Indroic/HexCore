"""
CORS de `create_app`: por fuera de la auth y del CSRF.

Una SPA en otro origen que le habla a la API con la cookie de sesión falla **en silencio** si el
CORS no las envuelve: `create_app` registraba el CORS **primero**, o sea que quedaba *por
dentro* de `AuthContextMiddleware` y de `CsrfMiddleware`. Un 403 de CSRF salía sin
`Access-Control-Allow-Origin` y el navegador lo convertía en un error de red opaco: la app no
podía leer *por qué* falló (ni reintentar con un token nuevo). El orden se prueba acá; que el
403 real lleve las cabeceras, en `test_darwin_http.py`.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from hexcore.config import ServerConfig  # noqa: E402
from hexcore.fastapi import AppFeatures, create_app  # noqa: E402

TENANT = "https://a.tenant.test"


def _nombres(app) -> list[str]:
    """De **afuera hacia adentro**: `user_middleware[0]` es el primero que ve el request."""
    return [m.cls.__name__ for m in app.user_middleware]


def _app_con_darwin(monkeypatch: pytest.MonkeyPatch):
    config = ServerConfig(allow_origins=[TENANT])
    monkeypatch.setattr("hexcore.infrastructure.api.app._config", lambda: config)
    return create_app(features=AppFeatures(auth_context=True, csrf=True))


def test_el_cors_envuelve_a_la_auth_y_al_csrf(monkeypatch):
    """
    Un middleware de Starlette sólo puede agregarle cabeceras a lo que responde **por dentro**
    de él. Si el CORS está adentro del CSRF, el 403 del CSRF no las lleva.
    """
    nombres = _nombres(_app_con_darwin(monkeypatch))

    assert nombres.index("CORSMiddleware") < nombres.index("AuthContextMiddleware")
    assert nombres.index("CORSMiddleware") < nombres.index("CsrfMiddleware")


def test_el_request_id_sigue_siendo_el_mas_externo(monkeypatch):
    """
    El request-id envuelve a todo lo demás para que su `ContextVar` esté disponible en la pila
    entera, y el 403 del CSRF —que ahora sale por dentro del CORS— lo sigue llevando.
    """
    assert _nombres(_app_con_darwin(monkeypatch))[0] == "RequestIDMiddleware"
