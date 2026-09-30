"""
CORS de `create_app`: por fuera de la auth y del CSRF, y con orígenes que no se enumeran.

Dos problemas del mismo caso de uso —una SPA en otro origen que le habla a la API con la cookie
de sesión—, y los dos fallan **en silencio**:

1. `create_app` registraba el CORS **primero**, o sea que quedaba *por dentro* de
   `AuthContextMiddleware` y de `CsrfMiddleware`. Un 403 de CSRF salía sin
   `Access-Control-Allow-Origin` y el navegador lo convertía en un error de red opaco: la app
   no podía leer *por qué* falló (ni reintentar con un token nuevo). El orden se prueba acá; que
   el 403 real lleve las cabeceras, en `test_darwin_http.py`.
2. `allow_origins` es una lista fija. Con un subdominio por tenant creado en caliente hay que
   redesplegar por cada alta, o abrir `"*"` —que con credenciales no es válido—. Un
   `cors_origin_predicate` cubre lo que la lista no enumera, igual que
   `IdentityConfig.trusted_origin_predicate` hace con el CSRF.

Además, sin `cors_max_age` cada escritura JSON con un header propio (`X-CSRF-Token`) cuesta un
`OPTIONS` previo sin caché, y sin `cors_expose_headers` el JS no puede leer las cabeceras de la
respuesta que la app le quiere entregar.
"""
from __future__ import annotations

import logging
import warnings

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402

from hexcore.config import ServerConfig  # noqa: E402
from hexcore.fastapi import AppFeatures, create_app  # noqa: E402
from hexcore.infrastructure.api.middlewares import PredicateCORSMiddleware  # noqa: E402

TENANT = "https://a.tenant.test"
AJENO = "https://evil.example"


def _es_de_un_tenant(origen: str) -> bool:
    return origen.endswith(".tenant.test")


def _app(monkeypatch: pytest.MonkeyPatch, **campos):
    """Una app con una `ServerConfig` propia (el `_config()` de `create_app` apunta a ella)."""
    config = ServerConfig(**campos)
    monkeypatch.setattr("hexcore.infrastructure.api.app._config", lambda: config)
    return create_app(), config


def _nombres(app) -> list[str]:
    """De **afuera hacia adentro**: `user_middleware[0]` es el primero que ve el request."""
    return [m.cls.__name__ for m in app.user_middleware]


# ── 1. Orden ───────────────────────────────────────────────────────────────────
def test_el_cors_envuelve_a_la_auth_y_al_csrf(monkeypatch):
    """
    Un middleware de Starlette sólo puede agregarle cabeceras a lo que responde **por dentro**
    de él. Si el CORS está adentro del CSRF, el 403 del CSRF no las lleva.
    """
    config = ServerConfig(allow_origins=[TENANT])
    monkeypatch.setattr("hexcore.infrastructure.api.app._config", lambda: config)

    app = create_app(features=AppFeatures(auth_context=True, csrf=True))
    nombres = _nombres(app)

    assert nombres.index("CORSMiddleware") < nombres.index("AuthContextMiddleware")
    assert nombres.index("CORSMiddleware") < nombres.index("CsrfMiddleware")


def test_el_request_id_sigue_siendo_el_mas_externo(monkeypatch):
    """
    El request-id envuelve a todo lo demás para que su `ContextVar` esté disponible en la pila
    entera, y el 403 del CSRF —que ahora sale por dentro del CORS— lo sigue llevando.
    """
    config = ServerConfig(allow_origins=[TENANT])
    monkeypatch.setattr("hexcore.infrastructure.api.app._config", lambda: config)

    app = create_app(features=AppFeatures(auth_context=True, csrf=True))

    assert _nombres(app)[0] == "RequestIDMiddleware"


def test_sin_predicado_el_middleware_es_el_de_starlette_tal_cual(monkeypatch):
    """Quien no pide un predicado no cambia de clase: el comportamiento es el de siempre."""
    app, _ = _app(monkeypatch, allow_origins=[TENANT])

    clases = [m.cls for m in app.user_middleware]

    assert CORSMiddleware in clases
    assert PredicateCORSMiddleware not in clases


# ── 2. cors_origin_predicate ───────────────────────────────────────────────────
def test_un_origen_que_el_predicado_acepta_recibe_cors_con_credenciales(monkeypatch):
    app, _ = _app(monkeypatch, allow_origins=[], cors_origin_predicate=_es_de_un_tenant)

    with TestClient(app) as client:
        r = client.get("/health", headers={"Origin": TENANT})

    assert r.headers["access-control-allow-origin"] == TENANT
    assert r.headers["access-control-allow-credentials"] == "true"


def test_un_origen_que_el_predicado_rechaza_no_recibe_cors(monkeypatch):
    app, _ = _app(monkeypatch, allow_origins=[], cors_origin_predicate=_es_de_un_tenant)

    with TestClient(app) as client:
        r = client.get("/health", headers={"Origin": AJENO})

    assert "access-control-allow-origin" not in r.headers


def test_el_preflight_de_un_origen_aceptado_pasa_y_el_de_uno_rechazado_no(monkeypatch):
    app, _ = _app(monkeypatch, allow_origins=[], cors_origin_predicate=_es_de_un_tenant)
    pedido = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type, x-csrf-token",
    }

    with TestClient(app) as client:
        bueno = client.options("/health", headers={"Origin": TENANT, **pedido})
        malo = client.options("/health", headers={"Origin": AJENO, **pedido})

    assert bueno.status_code == 200
    assert bueno.headers["access-control-allow-origin"] == TENANT
    assert malo.status_code == 400
    assert "access-control-allow-origin" not in malo.headers


def test_la_lista_fija_y_el_predicado_se_suman(monkeypatch):
    """La lista sigue mandando para lo que enumera; el predicado cubre el resto."""
    fijo = "https://portal.example"
    app, _ = _app(monkeypatch, allow_origins=[fijo], cors_origin_predicate=_es_de_un_tenant)

    with TestClient(app) as client:
        de_la_lista = client.get("/health", headers={"Origin": fijo})
        del_predicado = client.get("/health", headers={"Origin": TENANT})
        ajeno = client.get("/health", headers={"Origin": AJENO})

    assert de_la_lista.headers["access-control-allow-origin"] == fijo
    assert del_predicado.headers["access-control-allow-origin"] == TENANT
    assert "access-control-allow-origin" not in ajeno.headers


def test_un_predicado_que_lanza_se_trata_como_no_confiable(monkeypatch, caplog):
    """
    Falla cerrado, igual que `trusted_origin_predicate` y que un `AuthorizationProvider` que
    revienta: un chequeo de confianza que explota nunca decide «confío» por default. Y se
    loguea, porque el síntoma —un origen legítimo sin CORS— si no, no se diagnostica.
    """

    def explota(origen: str) -> bool:
        raise RuntimeError("la base de tenants no responde")

    app, _ = _app(monkeypatch, allow_origins=[], cors_origin_predicate=explota)

    with caplog.at_level(logging.ERROR, logger="hexcore.api.cors"):
        with TestClient(app) as client:
            r = client.get("/health", headers={"Origin": TENANT})

    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    assert any("cors_origin_predicate" in m for m in caplog.messages)


def test_la_respuesta_a_un_origen_rechazado_igual_declara_vary_origin(monkeypatch):
    """
    Con orígenes dinámicos la respuesta depende del `Origin` **también cuando se rechaza**. Sin
    `Vary: Origin`, una caché compartida podría servirle a un origen aceptado la respuesta que
    guardó sin `Access-Control-Allow-Origin`.
    """
    app, _ = _app(monkeypatch, allow_origins=[], cors_origin_predicate=_es_de_un_tenant)

    with TestClient(app) as client:
        r = client.get("/health", headers={"Origin": AJENO})

    assert "Origin" in r.headers["vary"]


def test_con_predicado_y_sin_allow_origins_no_se_deriva_el_comodin(monkeypatch):
    """
    El default de `allow_origins` en `debug` es `["*"]`, y con credenciales eso **baja
    `allow_credentials` a `False` en silencio** (con un warning). Con un predicado declarado, el
    predicado es la lista: derivar `["*"]` apagaría las cookies sin que nadie lo pidiera.
    """
    with warnings.catch_warnings(record=True) as avisos:
        warnings.simplefilter("always")
        config = ServerConfig(cors_origin_predicate=_es_de_un_tenant)

    assert config.allow_origins == []
    assert config.allow_credentials is True
    assert not [a for a in avisos if "allow_credentials" in str(a.message)]


def test_un_comodin_explicito_sigue_bajando_las_credenciales():
    """Lo que ya no era válido sigue sin serlo: el predicado no relaja esa regla."""
    with pytest.warns(UserWarning, match="allow_credentials"):
        config = ServerConfig(allow_origins=["*"], cors_origin_predicate=_es_de_un_tenant)

    assert config.allow_credentials is False


# ── 3. cors_max_age y cors_expose_headers ──────────────────────────────────────
def test_cors_max_age_llega_al_preflight(monkeypatch):
    app, _ = _app(monkeypatch, allow_origins=[TENANT], cors_max_age=7200)

    with TestClient(app) as client:
        r = client.options(
            "/health",
            headers={"Origin": TENANT, "Access-Control-Request-Method": "POST"},
        )

    assert r.headers["access-control-max-age"] == "7200"


def test_el_max_age_por_defecto_es_el_de_starlette(monkeypatch):
    """Quien no lo declara no ve cambiar nada: 600 s, el default de `CORSMiddleware`."""
    app, config = _app(monkeypatch, allow_origins=[TENANT])

    with TestClient(app) as client:
        r = client.options(
            "/health",
            headers={"Origin": TENANT, "Access-Control-Request-Method": "GET"},
        )

    assert config.cors_max_age == 600
    assert r.headers["access-control-max-age"] == "600"


def test_un_max_age_negativo_no_es_valido():
    with pytest.raises(ValidationError):
        ServerConfig(cors_max_age=-1)


def test_cors_expose_headers_se_declara_en_la_respuesta(monkeypatch):
    app, _ = _app(
        monkeypatch,
        allow_origins=[TENANT],
        cors_expose_headers=["X-Request-ID", "Retry-After"],
    )

    with TestClient(app) as client:
        r = client.get("/health", headers={"Origin": TENANT})

    assert r.headers["access-control-expose-headers"] == "X-Request-ID, Retry-After"
