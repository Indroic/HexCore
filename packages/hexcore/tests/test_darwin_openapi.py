"""
El contrato HTTP de Darwin, verificado desde la suite (Fase 0.3).

`scripts/darwin_openapi.py` es el que corre en CI y el que un desarrollador invoca a mano; este
archivo prueba las propiedades concretas que el plan promete, sin pasar por el subproceso ni
tocar disco — importa el script como módulo (mismo truco que `test_packaging.py` usa para
`extra_smoke`) y llama a sus funciones internas directamente.
"""
from __future__ import annotations

import sys

import pytest

from rutas import PAQUETE as REPO_ROOT

pytest.importorskip("fastapi")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")


@pytest.fixture
def darwin_openapi():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import darwin_openapi

        yield darwin_openapi
    finally:
        sys.path.pop(0)
        sys.modules.pop("darwin_openapi", None)

        from hexcore.darwin import reset_identity

        # `_construir_app()` deja el contenedor global cableado (configure_test_identity).
        # Sin este reset, el siguiente test de la suite hereda los plugins de éste.
        reset_identity()


#: Las 8 rutas del núcleo (build_identity_router), tal como aparecen en el OpenAPI.
RUTAS_DEL_NUCLEO = (
    ("/auth/sign-up", "post"),
    ("/auth/verify-email", "post"),
    ("/auth/sign-in", "post"),
    ("/auth/refresh", "post"),
    ("/auth/sign-out", "post"),
    ("/auth/sign-out-everywhere", "post"),
    ("/auth/me", "get"),
    ("/auth/sessions", "get"),
)


class TestElContratoVolcado:
    def test_las_ocho_rutas_del_nucleo_tienen_response_model(self, darwin_openapi):
        app = darwin_openapi._construir_app()
        schema = app.openapi()

        faltantes = []
        for ruta, metodo in RUTAS_DEL_NUCLEO:
            operacion = schema["paths"].get(ruta, {}).get(metodo)
            if operacion is None:
                faltantes.append(f"{metodo.upper()} {ruta}: no está en el schema")
                continue
            respuestas_ok = {
                codigo: cuerpo
                for codigo, cuerpo in operacion["responses"].items()
                if codigo.startswith(("2",))
            }
            tiene_schema = any(
                "content" in cuerpo
                and cuerpo["content"].get("application/json", {}).get("schema")
                for cuerpo in respuestas_ok.values()
            )
            if not tiene_schema:
                faltantes.append(f"{metodo.upper()} {ruta}: sin schema de respuesta")

        assert not faltantes, (
            f"estas rutas del núcleo no tienen `response_model`: {faltantes}"
        )

    def test_el_cuerpo_de_sign_in_publica_los_tres_nombres(self, darwin_openapi):
        """
        El contrato es lo que el cliente TypeScript genera, así que si los alias no salen acá,
        un front que manda `{"email": ...}` queda sin tipo aunque el backend lo acepte.

        Los tres son **opcionales** en el schema: cuál hace falta lo decide un validador, no la
        forma. Un `required: [email]` heredado de 9.x rompería al que manda `identifier`.
        """
        schema = darwin_openapi._construir_app().openapi()
        cuerpo = schema["components"]["schemas"]["SignInRequest"]

        for nombre in ("identifier", "email", "username"):
            assert nombre in cuerpo["properties"], (
                f"'{nombre}' no está en el cuerpo de sign-in; un cliente que lo mande queda "
                f"sin tipo generado."
            )
        assert cuerpo.get("required", []) == ["password"], (
            "Sólo `password` es obligatorio: el identificador puede venir con cualquiera de "
            f"los tres nombres. Requeridos declarados: {cuerpo.get('required')}"
        )

    def test_el_alta_admite_username_y_un_mail_opcional(self, darwin_openapi):
        """El cambio de 10.0: se puede crear una cuenta sin dirección de correo."""
        schema = darwin_openapi._construir_app().openapi()
        cuerpo = schema["components"]["schemas"]["SignUpRequest"]

        assert "username" in cuerpo["properties"]
        assert "email" not in cuerpo.get("required", []), (
            "Si `email` sigue siendo obligatorio en el contrato, el cliente no puede expresar "
            "un alta sólo con username."
        )

    def test_me_devuelve_el_username(self, darwin_openapi):
        schema = darwin_openapi._construir_app().openapi()

        assert "username" in schema["components"]["schemas"]["MeResponse"]["properties"]

    def test_el_schema_tiene_los_dos_securityschemes(self, darwin_openapi):
        app = darwin_openapi._construir_app()
        schema = app.openapi()
        darwin_openapi._inyectar_seguridad(app, schema)

        esquemas = schema["components"]["securitySchemes"]
        assert esquemas["darwinBearer"] == {"type": "http", "scheme": "bearer"}
        assert esquemas["darwinCookie"]["type"] == "apiKey"

    def test_una_ruta_protegida_lleva_security_y_una_publica_no(self, darwin_openapi):
        app = darwin_openapi._construir_app()
        schema = app.openapi()
        darwin_openapi._inyectar_seguridad(app, schema)

        assert "security" in schema["paths"]["/auth/me"]["get"], (
            "/auth/me exige provide_auth y tiene que llevar `security`"
        )
        assert "security" not in schema["paths"]["/auth/sign-in"]["post"], (
            "/auth/sign-in es pública: no debería llevar `security`"
        )

    def test_los_errores_combinados_incluyen_nucleo_y_plugins(self, darwin_openapi):
        # Se descarta la app: sólo hace falta que `_construir_app()` deje el contenedor
        # global configurado, que es de donde `_errores_combinados()` lee los plugins.
        darwin_openapi._construir_app()
        errores = darwin_openapi._errores_combinados()

        # Del núcleo.
        assert errores["UnauthenticatedError"] == 401
        # De un plugin (two_factor).
        assert errores["TwoFactorRequiredError"] == 401

    def test_write_y_check_son_deterministas(self, darwin_openapi, tmp_path, monkeypatch):
        """
        `--check` inmediatamente después de `--write` tiene que pasar: si no, el volcado no
        es determinista y el gate de CI parpadearía entre verde y rojo sin que nada haya
        cambiado en el repo.
        """
        monkeypatch.setattr(darwin_openapi, "OUT_DIR", tmp_path)

        monkeypatch.setattr(sys, "argv", ["darwin_openapi.py", "--write"])
        assert darwin_openapi.main() == 0

        monkeypatch.setattr(sys, "argv", ["darwin_openapi.py", "--check"])
        assert darwin_openapi.main() == 0
