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


# ── Derivación del default ────────────────────────────────────────────────────
def test_debug_true_permite_comodin():
    """En desarrollo el default sigue siendo `["*"]`: era la intención original."""
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


# ── Fail-fast de la combinación insegura ──────────────────────────────────────
def test_comodin_con_credenciales_sin_debug_no_arranca():
    with pytest.raises(ValueError) as excinfo:
        ServerConfig(debug=False, allow_origins=["*"])

    mensaje = str(excinfo.value)
    assert "allow_credentials" in mensaje
    assert "allow_origins" in mensaje


def test_comodin_sin_credenciales_es_valido():
    """Una API pública sin cookies puede usar `["*"]`: ahí no hay nada que robar."""
    config = ServerConfig(
        debug=False, allow_origins=["*"], allow_credentials=False
    )

    assert config.allow_origins == ["*"]


def test_comodin_con_credenciales_en_debug_avisa_pero_arranca():
    with pytest.warns(UserWarning, match="reflejar"):
        ServerConfig(debug=True, allow_origins=["*"])


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
