"""
Fase 0: `allow_origins` no puede volver a ser `["*"]` con credenciales.

El bug: `ServerConfig.allow_origins` se declaraba como
``["*" if debug else "http://localhost:{port}"]`` en el cuerpo de la clase, donde `debug`
es el nombre del cuerpo de clase —siempre `True`—, así que el condicional era código
muerto y el valor era **siempre** `["*"]`, incluso con `ServerConfig(debug=False)`.

Por qué importa: Starlette no puede emitir `Access-Control-Allow-Origin: *` junto con
credenciales, así que cuando el request trae cookie **refleja el Origin de quien
pregunte** y agrega `Access-Control-Allow-Credentials: true`. Cualquier origen puede
entonces leer respuestas autenticadas con la cookie de sesión de la víctima, sin XSS.
Hoy es latente porque HexCore no tiene sesiones por cookie; el módulo de identidad lo
volvería explotable, así que se arregla antes.
"""
from __future__ import annotations

import warnings

import pytest

from hexcore.config import ServerConfig


# ── La configuración de fábrica es segura ─────────────────────────────────────
def test_la_config_por_defecto_no_es_explotable():
    """
    El agujero real: `debug` viene en `True`, así que condicionar el invariante al entorno
    dejaba la combinación peligrosa como configuración **de fábrica** — `create_app()` sin
    tocar nada reflejaba el Origin del atacante con credenciales.

    `["*"]` se mantiene por comodidad, pero sin credenciales: sin
    `Access-Control-Allow-Credentials: true` el navegador no expone la respuesta a una
    petición con cookies, así que el reflejo queda inofensivo.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = ServerConfig()

    assert config.allow_origins == ["*"]
    assert config.allow_credentials is False


def test_declarar_origenes_mantiene_las_credenciales():
    """El camino para sesiones por cookie: declarás tus orígenes y las cookies funcionan."""
    config = ServerConfig(allow_origins=["http://localhost:3000"], allow_credentials=True)

    assert config.allow_credentials is True


def test_debug_true_permite_comodin():
    """En desarrollo el default de orígenes sigue siendo `["*"]`."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert ServerConfig().allow_origins == ["*"]


def test_debug_false_no_es_comodin():
    """La regresión concreta: sin debug, el default deja de ser `["*"]`."""
    config = ServerConfig(debug=False)

    assert config.allow_origins != ["*"]
    assert "*" not in config.allow_origins


def test_debug_false_deriva_del_port_de_la_instancia():
    """El `port` que se usa es el de la instancia, no el del cuerpo de clase."""
    assert ServerConfig(debug=False, port=9001).allow_origins == [
        "http://localhost:9001"
    ]


def test_un_valor_explicito_no_se_toca():
    config = ServerConfig(debug=False, allow_origins=["https://front.example"])

    assert config.allow_origins == ["https://front.example"]


def test_una_lista_vacia_explicita_se_respeta():
    """`[]` es una elección válida —bloquear todo cross-origin— y no se autocompleta."""
    assert ServerConfig(debug=False, allow_origins=[]).allow_origins == []


# ── El invariante: "*" con credenciales nunca es válido ───────────────────────
def test_pedir_las_dos_cosas_explicitamente_no_arranca():
    """
    Si declaraste `["*"]` **y** `allow_credentials=True`, no se adivina cuál querías.

    La especificación de CORS no permite esa combinación, así que fallar es más honesto
    que elegir por vos.
    """
    with pytest.raises(ValueError) as excinfo:
        ServerConfig(allow_origins=["*"], allow_credentials=True)

    mensaje = str(excinfo.value)
    assert "allow_credentials" in mensaje
    assert "allow_origins" in mensaje


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"allow_origins": ["*"]}, id="origins-explicito"),
        pytest.param({"allow_credentials": True}, id="credentials-explicito"),
        pytest.param({}, id="ninguno-explicito"),
    ],
)
def test_si_una_de_las_dos_vino_por_default_se_baja_credentials(kwargs):
    """Con una sola declarada se degrada y se avisa, en vez de negarse a arrancar."""
    with pytest.warns(UserWarning, match="allow_credentials"):
        config = ServerConfig(**kwargs)

    assert config.allow_credentials is False


def test_comodin_sin_credenciales_es_valido():
    """Una API pública sin cookies puede usar `["*"]`: ahí no hay nada que robar."""
    config = ServerConfig(
        debug=False, allow_origins=["*"], allow_credentials=False
    )

    assert config.allow_origins == ["*"]
    assert config.allow_credentials is False


# ── Regresión end-to-end contra la app real ───────────────────────────────────
def test_create_app_no_refleja_el_origin_del_atacante():
    """
    Antes de este fix devolvía ``ACAO: https://evil.example`` + ``ACAC: true``.

    Se ejerce el camino completo: `ServerConfig` → `create_app` → `CORSMiddleware`, con
    cookie, que es la rama de Starlette que refleja.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from fastapi.testclient import TestClient

    from hexcore.config import LazyConfig
    from hexcore.infrastructure.api.app import create_app

    previo = LazyConfig._imported_config
    LazyConfig._imported_config = ServerConfig(debug=False)
    try:
        client = TestClient(create_app())
        response = client.get(
            "/health/live",
            headers={"Origin": "https://evil.example", "Cookie": "sesion=1"},
        )
    finally:
        LazyConfig._imported_config = previo

    assert response.headers.get("access-control-allow-origin") != "https://evil.example"


def test_create_app_con_la_config_de_fabrica_no_expone_credenciales():
    """
    El caso que de verdad importaba: `create_app()` **sin tocar nada**.

    `LazyConfig` devuelve un `ServerConfig()` con `debug=True`, así que condicionar el
    invariante a `debug` dejaba la app de fábrica reflejando el Origin del atacante con
    `Access-Control-Allow-Credentials: true`. Ahora puede seguir reflejando —`["*"]` sigue
    siendo el default de desarrollo— pero **sin** credenciales, y sin ellas el navegador
    no expone la respuesta a una petición con cookies.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from fastapi.testclient import TestClient

    from hexcore.config import LazyConfig
    from hexcore.infrastructure.api.app import create_app

    previo = LazyConfig._imported_config
    LazyConfig.clear_cache()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            app = create_app()
        response = TestClient(app).get(
            "/health/live",
            headers={"Origin": "https://evil.example", "Cookie": "sesion=1"},
        )
    finally:
        LazyConfig._imported_config = previo

    assert response.headers.get("access-control-allow-credentials") != "true"
