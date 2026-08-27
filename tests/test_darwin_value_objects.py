"""
Darwin: value objects, y los cuatro defectos de `TokenClaims` que `AccessTokenClaims` corrige.

El `TokenClaims` que ya se shippea (`hexcore.domain.auth.value_objects`) tiene:

1. `client_id: str` obligatorio → no puede representar un token de sesión de primera parte.
2. `scopes: t.List[Enum] = []` → default mutable, y `Enum` pelado no sobrevive un
   `model_dump(mode="json")`.
3. **Sin `sid`** → la revocación es imposible por construcción.
4. Sin `aud` / `nbf` / `typ` → sin `aud` no se distingue el transporte (una cookie se puede
   replayear como Bearer); sin `typ`, un refresh se puede presentar donde va un access.

El tercero es el descalificante y por eso `sid` acá es obligatorio.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hexcore.darwin import AccessTokenClaims, Email, TokenPair

AHORA = int(datetime(2026, 8, 6, 12, 0, tzinfo=UTC).timestamp())


def _claims(**overrides) -> AccessTokenClaims:
    usuario = overrides.pop("_usuario", None) or uuid4()
    base = {
        "iss": "https://api.ejemplo.com",
        "sub": usuario,
        "act": usuario,
        "sid": uuid4(),
        "aud": "cookie",
        "typ": "at+jwt",
        "iat": AHORA,
        "nbf": AHORA,
        "exp": AHORA + 120,
    }
    base.update(overrides)
    return AccessTokenClaims(**base)


# ── Los cuatro defectos corregidos ────────────────────────────────────────────
def test_no_hace_falta_un_client_id():
    """Defecto 1: un token de sesión de primera parte no tiene client id de OAuth."""
    claims = _claims()

    assert not hasattr(claims, "client_id")


def test_los_scopes_son_un_frozenset_serializable():
    """
    Defecto 2: `List[Enum] = []` era default mutable y no round-trippeaba a JSON.
    """
    claims = _claims(scopes=frozenset({"users.view", "users.edit"}))

    volcado = claims.model_dump(mode="json")

    assert sorted(volcado["scopes"]) == ["users.edit", "users.view"]
    # Y round-trippea.
    assert AccessTokenClaims.model_validate(volcado).scopes == claims.scopes


def test_los_scopes_no_comparten_default_entre_instancias():
    a, b = _claims(), _claims()

    assert a.scopes is b.scopes or a.scopes == frozenset()
    assert isinstance(a.scopes, frozenset)


def test_el_sid_es_obligatorio():
    """
    Defecto 3, el descalificante: sin `sid` el token no se puede atar a una fila de sesión,
    así que **no hay revocación posible**.
    """
    with pytest.raises(ValidationError) as excinfo:
        AccessTokenClaims(
            iss="i",
            sub=uuid4(),
            act=uuid4(),
            aud="cookie",
            typ="at+jwt",
            iat=AHORA,
            nbf=AHORA,
            exp=AHORA + 60,
        )

    assert "sid" in str(excinfo.value)


def test_el_typ_distingue_access_de_refresh():
    """Defecto 4a: sin `typ`, un refresh se presenta donde se espera un access."""
    assert _claims(typ="at+jwt").is_access is True
    assert _claims(typ="rt+jwt").is_access is False

    with pytest.raises(ValidationError):
        _claims(typ="cualquiera")


def test_el_aud_ata_el_token_a_su_transporte():
    """Defecto 4b: sin `aud`, una cookie se replayea como Bearer y esquiva CSRF."""
    assert _claims(aud="cookie").aud == "cookie"
    assert _claims(aud="bearer").aud == "bearer"


def test_el_nbf_esta_presente():
    assert _claims().nbf == AHORA


# ── Coherencia temporal ───────────────────────────────────────────────────────
def test_exp_tiene_que_ser_posterior_a_iat():
    with pytest.raises(ValidationError, match="posterior a iat"):
        _claims(exp=AHORA - 1)


def test_nbf_no_puede_ser_posterior_a_exp():
    """Un token que nunca sería válido es un bug de emisión, no un token."""
    with pytest.raises(ValidationError, match="nunca sería válido"):
        _claims(nbf=AHORA + 500, exp=AHORA + 100)


def test_is_expired_at_respeta_el_leeway():
    claims = _claims(exp=AHORA + 120)
    justo_despues = datetime.fromtimestamp(AHORA + 121, tz=UTC)

    assert claims.is_expired_at(justo_despues) is True
    # El leeway es para el desfase de reloj entre el que emite y el worker que verifica.
    assert claims.is_expired_at(justo_despues, leeway=timedelta(seconds=30)) is False


# ── El invariante de impersonación, también en el token ───────────────────────
def test_act_distinto_de_sub_exige_la_marca_imp():
    """
    Si el flag y los ids no concuerdan, algo re-derivó los claims y perdió al actor — que es
    exactamente el modo de falla por el que la impersonación se escapa de la auditoría.
    """
    with pytest.raises(ValidationError, match="perdió la marca"):
        _claims(sub=uuid4(), act=uuid4(), imp=False)


def test_la_marca_imp_exige_que_act_y_sub_difieran():
    usuario = uuid4()

    with pytest.raises(ValidationError, match="act y sub son el mismo"):
        _claims(_usuario=usuario, imp=True)


def test_una_impersonacion_coherente_es_valida():
    claims = _claims(sub=uuid4(), act=uuid4(), imp=True)

    assert claims.imp is True
    assert claims.act != claims.sub


def test_los_claims_son_inmutables():
    """Un claim set mutable invita a "arreglar" un token verificado en memoria."""
    claims = _claims()

    with pytest.raises(ValidationError):
        claims.exp = AHORA + 99999  # type: ignore[misc]


# ── Email ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("  Ana@Example.COM ", "ana@example.com"),
        ("ANA@EXAMPLE.COM", "ana@example.com"),
        ("ana@example.com", "ana@example.com"),
    ],
)
def test_el_mail_se_normaliza(entrada, esperado):
    """
    Sin normalizar, `Ana@Example.com` y `ana@example.com` crean dos cuentas y el login es
    una lotería según cómo lo tipeó el usuario.
    """
    assert Email(value=entrada).value == esperado


def test_no_se_normaliza_mas_alla_de_eso():
    """
    Ni sacar puntos de gmail ni cortar el sufijo `+tag`: son políticas de cada proveedor,
    cambian, y aplicarlas de prepo impide usar un alias legítimo para separar cuentas.
    """
    assert Email(value="ana.gomez+facturas@gmail.com").value == (
        "ana.gomez+facturas@gmail.com"
    )


@pytest.mark.parametrize(
    "invalido", ["sin-arroba", "@sindominio.com", "ana@", "ana@sinpunto", "a b@c.com"]
)
def test_un_mail_sin_forma_de_mail_se_rechaza(invalido):
    with pytest.raises(ValidationError):
        Email(value=invalido)


def test_el_dominio_se_expone():
    assert Email(value="ana@Example.com").domain == "example.com"


# ── TokenPair ─────────────────────────────────────────────────────────────────
def test_el_refresh_es_opcional():
    """
    El transporte por cookie no lo pone en el cuerpo: va en su propia cookie `HttpOnly`. Un
    cliente Bearer sí lo recibe, porque no tiene dónde más guardarlo.
    """
    solo_cookie = TokenPair(access_token="a", expires_in=120, session_id=uuid4())
    con_refresh = TokenPair(
        access_token="a", refresh_token="r", expires_in=120, session_id=uuid4()
    )

    assert solo_cookie.refresh_token is None
    assert con_refresh.refresh_token == "r"
