"""
Darwin Fase 4: el núcleo de seguridad. Tests adversariales de tokens y claves.

Cada test acá corresponde a un ataque concreto y publicado, no a una rama de código:

- **Confusión de algoritmo**: `alg: none`, y HS256 firmado con la clave *pública* Ed25519 como
  secreto HMAC. Es el ataque clásico contra JWT y sigue funcionando en librerías que aceptan el
  `alg` del token.
- **Confusión de `typ`**: presentar un refresh donde va un access. Sin el chequeo, el TTL de
  120 s del access deja de servir: se usa el refresh, que vive 30 días.
- **Confusión de transporte**: replayear una cookie como `Authorization: Bearer`, esquivando
  `SameSite` y CSRF de una sola vez.
- **`kid` desconocido / retirado**, con caché negativa contada bajo flood.
- **Rotación**: un token de la clave anterior tiene que seguir verificando en `verify_only` y
  fallar recién en `retired`.

El reloj es un `FixedClock` inyectado: **no se agrega `freezegun` ni `time-machine`**.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytest.importorskip("joserfc")

from hexcore.darwin import (  # noqa: E402
    AuthContext,
    FixedClock,
    Principal,
    SystemPrincipal,
    TokenAudienceMismatchError,
    TokenExpiredError,
    TokenMalformedError,
    generate_signing_key,
)
from hexcore.darwin.infrastructure.keys import (  # noqa: E402
    NoActiveKeyError,
    RetiredKeyError,
    StaticKeyStore,
    UnknownKeyError,
    jwks_document,
)
from hexcore.darwin.infrastructure.tokens import (  # noqa: E402
    JoserfcTokenIssuer,
    JoserfcTokenVerifier,
    audience_for,
)

EMISOR = "https://api.ejemplo.com"
AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def almacen() -> StaticKeyStore:
    return StaticKeyStore([generate_signing_key(kid="k1")])


@pytest.fixture
def emisor(almacen, reloj) -> JoserfcTokenIssuer:
    return JoserfcTokenIssuer(issuer=EMISOR, key_store=almacen, clock=reloj)


@pytest.fixture
def verificador(almacen, reloj) -> JoserfcTokenVerifier:
    return JoserfcTokenVerifier(issuer=EMISOR, key_store=almacen, clock=reloj)


@pytest.fixture
def contexto() -> AuthContext:
    principal = Principal(user_id=uuid4(), session_id=uuid4())
    return AuthContext(actor=principal, subject=principal, transport="cookie")


def _cabecera(token: str) -> dict:
    crudo = token.split(".", 1)[0]
    return json.loads(base64.urlsafe_b64decode(crudo + "=" * (-len(crudo) % 4)))


# ── Camino feliz ──────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_un_token_emitido_verifica(emisor, verificador, contexto):
    sid = uuid4()
    token = await emisor.issue_access(contexto, session_id=sid, scopes={"users.view"})

    claims = await verificador.verify(token, transport="cookie")

    assert claims.sid == sid
    assert claims.sub == contexto.subject.user_id
    assert claims.act == contexto.actor.user_id
    assert claims.typ == "at+jwt"
    assert claims.scopes == {"users.view"}
    assert claims.imp is False


@pytest.mark.anyio
async def test_el_algoritmo_por_defecto_no_es_el_deprecado(emisor, contexto):
    """
    RFC 9864 deprecó el identificador `EdDSA` en favor del nombre de la curva, y `joserfc`
    emite un `SecurityWarning` si se usa. Nacer con un algoritmo deprecado es deuda caro de
    migrar: los tokens ya emitidos llevan el `alg` viejo en la cabecera.
    """
    token = await emisor.issue_access(contexto, session_id=uuid4())

    assert _cabecera(token)["alg"] == "Ed25519"


@pytest.mark.anyio
async def test_la_impersonacion_sobrevive_al_token(emisor, verificador):
    """
    `act` y `sub` van por separado. Derivarlos los dos del sujeto perdería al actor, y a partir
    de ahí la acción queda atribuida a la víctima.
    """
    from datetime import timedelta

    from hexcore.darwin import Impersonation

    operador = Principal(user_id=uuid4())
    cliente = Principal(user_id=uuid4())
    contexto = AuthContext(
        actor=operador,
        subject=cliente,
        transport="cookie",
        impersonation=Impersonation(
            granted_by=uuid4(),
            reason="ticket #4821",
            granted_at=AHORA,
            expires_at=AHORA + timedelta(minutes=60),
        ),
    )

    token = await emisor.issue_access(contexto, session_id=uuid4())
    claims = await verificador.verify(token, transport="cookie")

    assert claims.act == operador.user_id
    assert claims.sub == cliente.user_id
    assert claims.imp is True


# ── Confusión de algoritmo ────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_alg_none_se_rechaza(emisor, verificador, contexto):
    """
    El ataque más viejo contra JWT: reescribir la cabecera a `alg: none` y sacar la firma.

    Se rechaza porque `none` no está —ni puede estar— en la allowlist.
    """
    token = await emisor.issue_access(contexto, session_id=uuid4())
    cabecera, payload, _ = token.split(".")

    manipulada = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "kid": "k1"}).encode()
    ).rstrip(b"=").decode()
    sin_firma = f"{manipulada}.{payload}."

    with pytest.raises(TokenMalformedError, match="algoritmo"):
        await verificador.verify(sin_firma, transport="cookie")


@pytest.mark.anyio
async def test_hs256_firmado_con_la_clave_publica_se_rechaza(almacen, verificador):
    """
    **La confusión de algoritmo clásica.**

    La clave pública Ed25519 es pública por definición. Si el verificador aceptara `HS256`,
    el atacante la usa como secreto HMAC, firma lo que quiera y el token verifica.

    Se corta en dos lugares: `HS256` no está en la allowlist, y el `alg` del token tiene que
    coincidir con el de la clave que su `kid` señala.
    """
    from joserfc import jwt
    from joserfc.jwk import KeySet, OctKey

    clave = await almacen.get("k1")
    assert clave is not None

    # El atacante toma el material público —la componente `x` del JWK, que es pública por
    # definición— y lo usa como secreto HMAC, con el `kid` de la clave legítima.
    publica = json.loads(clave.public_key)
    publica_como_secreto = OctKey.import_key(
        {"kty": "oct", "k": publica["x"], "kid": "k1"}
    )
    forjado = jwt.encode(
        {"alg": "HS256", "kid": "k1"},
        {
            "iss": EMISOR,
            "sub": str(uuid4()),
            "act": str(uuid4()),
            "sid": str(uuid4()),
            "aud": audience_for(EMISOR, "cookie"),
            "typ": "at+jwt",
            "gen": 0,
            "iat": int(AHORA.timestamp()),
            "nbf": int(AHORA.timestamp()),
            "exp": int(AHORA.timestamp()) + 3600,
            "imp": True,
        },
        KeySet([publica_como_secreto]),
        algorithms=["HS256"],
    )

    with pytest.raises(TokenMalformedError, match="algoritmo"):
        await verificador.verify(forjado, transport="cookie")


@pytest.mark.anyio
async def test_un_algoritmo_fuera_de_la_allowlist_se_rechaza(almacen, reloj, contexto):
    """El verificador manda: si sólo permite Ed25519, un RS256 legítimo también se rechaza."""
    emisor = JoserfcTokenIssuer(issuer=EMISOR, key_store=almacen, clock=reloj)
    restrictivo = JoserfcTokenVerifier(
        issuer=EMISOR, key_store=almacen, clock=reloj, allowed_algorithms=["RS256"]
    )
    token = await emisor.issue_access(contexto, session_id=uuid4())

    with pytest.raises(TokenMalformedError, match="algoritmo"):
        await restrictivo.verify(token, transport="cookie")


def test_incluir_hs_en_la_allowlist_avisa(almacen, reloj):
    """
    No se prohíbe —hay despliegues de un solo servicio donde HS* es razonable— pero tiene que
    ser una decisión consciente, porque habilita la confusión de algoritmo.
    """
    with pytest.warns(UserWarning, match="HS"):
        JoserfcTokenVerifier(
            issuer=EMISOR,
            key_store=almacen,
            clock=reloj,
            allowed_algorithms=["Ed25519", "HS256"],
        )


# ── Confusión de tipo y de transporte ─────────────────────────────────────────
@pytest.mark.anyio
async def test_un_refresh_no_pasa_donde_va_un_access(emisor, verificador, contexto):
    """Sin este chequeo, el TTL de 120 s del access no sirve: se usa el refresh de 30 días."""
    refresh = await emisor.issue_refresh(contexto, session_id=uuid4())

    with pytest.raises(TokenMalformedError, match="tipo"):
        await verificador.verify(refresh, transport="cookie", expected_typ="at+jwt")

    # Y sí pasa donde corresponde.
    claims = await verificador.verify(
        refresh, transport="cookie", expected_typ="rt+jwt"
    )
    assert claims.typ == "rt+jwt"


@pytest.mark.anyio
async def test_un_refresh_no_lleva_scopes(emisor, verificador, contexto):
    """Un refresh no autoriza nada, sólo canjea. Con permisos, uno robado sirve para actuar."""
    refresh = await emisor.issue_refresh(contexto, session_id=uuid4())

    claims = await verificador.verify(refresh, transport="cookie", expected_typ="rt+jwt")

    assert claims.scopes == frozenset()


@pytest.mark.anyio
async def test_una_cookie_no_se_puede_presentar_como_bearer(emisor, verificador, contexto):
    """
    **El ataque que el `aud` corta.**

    Una cookie replayeada como `Authorization: Bearer` esquiva `SameSite` y el chequeo
    anti-CSRF de una sola vez, porque el camino Bearer no los aplica.
    """
    token = await emisor.issue_access(contexto, session_id=uuid4())

    with pytest.raises(TokenAudienceMismatchError):
        await verificador.verify(token, transport="bearer")


@pytest.mark.anyio
async def test_un_emisor_distinto_se_rechaza(almacen, reloj, emisor, contexto):
    """Un token de otro servicio que comparta el almacén de claves no debe pasar."""
    otro = JoserfcTokenVerifier(
        issuer="https://otra-api.ejemplo.com", key_store=almacen, clock=reloj
    )
    token = await emisor.issue_access(contexto, session_id=uuid4())

    with pytest.raises(TokenMalformedError, match="emisor"):
        await otro.verify(token, transport="cookie")


# ── Ventana temporal ──────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_un_token_vencido_se_rechaza(emisor, verificador, contexto, reloj):
    token = await emisor.issue_access(contexto, session_id=uuid4())

    reloj.advance(seconds=121 + 31)  # TTL + leeway

    with pytest.raises(TokenExpiredError):
        await verificador.verify(token, transport="cookie")


@pytest.mark.anyio
async def test_el_leeway_cubre_el_desfase_de_reloj(emisor, verificador, contexto, reloj):
    """
    El emisor y el worker que verifica pueden tener relojes distintos. El margen se aplica
    **sólo** a la ventana temporal, nunca a la revocación.
    """
    token = await emisor.issue_access(contexto, session_id=uuid4())

    reloj.advance(seconds=125)  # pasado el exp, dentro del leeway de 30 s

    claims = await verificador.verify(token, transport="cookie")
    assert claims.typ == "at+jwt"


@pytest.mark.anyio
async def test_un_nbf_futuro_se_rechaza(almacen, reloj, contexto):
    """Un token emitido con fecha futura por un reloj desincronizado no debe pasar todavía."""
    futuro = FixedClock(AHORA)
    futuro.advance(minutes=10)
    emisor_futuro = JoserfcTokenIssuer(issuer=EMISOR, key_store=almacen, clock=futuro)
    verificador = JoserfcTokenVerifier(issuer=EMISOR, key_store=almacen, clock=reloj)

    token = await emisor_futuro.issue_access(contexto, session_id=uuid4())

    with pytest.raises(TokenMalformedError, match="nbf"):
        await verificador.verify(token, transport="cookie")


# ── kid ───────────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_un_token_sin_kid_se_rechaza(verificador):
    cabecera = base64.urlsafe_b64encode(json.dumps({"alg": "Ed25519"}).encode()).rstrip(
        b"="
    ).decode()

    with pytest.raises(TokenMalformedError, match="kid"):
        await verificador.verify(f"{cabecera}.e30.x", transport="cookie")


@pytest.mark.anyio
async def test_un_kid_desconocido_se_rechaza(emisor, verificador, contexto, almacen):
    token = await emisor.issue_access(contexto, session_id=uuid4())
    cabecera, payload, firma = token.split(".")
    otra = base64.urlsafe_b64encode(
        json.dumps({"alg": "Ed25519", "kid": "inventado"}).encode()
    ).rstrip(b"=").decode()

    with pytest.raises(UnknownKeyError):
        await verificador.verify(f"{otra}.{payload}.{firma}", transport="cookie")


@pytest.mark.anyio
async def test_la_cache_negativa_frena_el_flood_de_kids(almacen, reloj):
    """
    Un flood de `kid` inventados sería un `SELECT` por petición contra el almacén: un ataque
    de amplificación gratis. Se cuenta cuántas veces se consulta el almacén.
    """
    consultas: list[str] = []

    class AlmacenContado(StaticKeyStore):
        async def get(self, kid: str):
            consultas.append(kid)
            return await super().get(kid)

    contado = AlmacenContado([generate_signing_key(kid="k1")])
    verificador = JoserfcTokenVerifier(issuer=EMISOR, key_store=contado, clock=reloj)

    cabecera = base64.urlsafe_b64encode(
        json.dumps({"alg": "Ed25519", "kid": "inventado"}).encode()
    ).rstrip(b"=").decode()
    token = f"{cabecera}.e30.x"

    for _ in range(25):
        with pytest.raises(UnknownKeyError):
            await verificador.verify(token, transport="cookie")

    # 25 intentos, una sola consulta: el resto lo atajó la caché negativa.
    assert len(consultas) == 1


@pytest.mark.anyio
async def test_una_clave_retirada_no_verifica(emisor, verificador, contexto, almacen):
    token = await emisor.issue_access(contexto, session_id=uuid4())
    await almacen.retire("k1")

    with pytest.raises(RetiredKeyError):
        await verificador.verify(token, transport="cookie")


# ── Rotación ──────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_la_rotacion_no_invalida_los_tokens_en_vuelo(
    emisor, verificador, contexto, almacen
):
    """
    **La razón de existir del estado `verify_only`.**

    Sin él, rotar invalida de golpe todo token en vuelo: cada sesión activa recibe un 401 y el
    usuario ve un logout masivo inexplicable.
    """
    viejo = await emisor.issue_access(contexto, session_id=uuid4())

    nueva = await almacen.rotate()

    # El token de la clave anterior sigue verificando…
    assert (await verificador.verify(viejo, transport="cookie")).typ == "at+jwt"
    # …y los nuevos se firman con la nueva.
    reciente = await emisor.issue_access(contexto, session_id=uuid4())
    assert _cabecera(reciente)["kid"] == nueva.kid


@pytest.mark.anyio
async def test_retirar_la_clave_vieja_si_invalida_sus_tokens(
    emisor, verificador, contexto, almacen
):
    viejo = await emisor.issue_access(contexto, session_id=uuid4())
    await almacen.rotate()
    await almacen.retire("k1")

    with pytest.raises(RetiredKeyError):
        await verificador.verify(viejo, transport="cookie")


@pytest.mark.anyio
async def test_sin_clave_activa_no_se_puede_firmar(reloj, contexto):
    vacio = StaticKeyStore()
    emisor = JoserfcTokenIssuer(issuer=EMISOR, key_store=vacio, clock=reloj)

    with pytest.raises(NoActiveKeyError, match="active"):
        await emisor.issue_access(contexto, session_id=uuid4())


# ── JWKS ──────────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_el_jwks_no_publica_material_privado(almacen):
    documento = await jwks_document(almacen)

    assert len(documento["keys"]) == 1
    for clave in documento["keys"]:
        for privado in ("d", "p", "q", "dp", "dq", "qi", "k"):
            assert privado not in clave, f"el JWKS publicó el componente privado {privado!r}"
        assert clave["use"] == "sig"
        assert clave["kid"] == "k1"


@pytest.mark.anyio
async def test_el_jwks_excluye_las_retiradas(almacen):
    await almacen.rotate()
    await almacen.retire("k1")

    documento = await jwks_document(almacen)

    assert "k1" not in {c["kid"] for c in documento["keys"]}


@pytest.mark.anyio
async def test_el_jwks_nunca_publica_claves_simetricas():
    """
    En una clave simétrica el "material público" **es** el secreto de firma: publicarlo le
    permite a cualquiera emitir tokens válidos.
    """
    simetrico = StaticKeyStore([generate_signing_key(kid="hs", algorithm="HS256")])

    documento = await jwks_document(simetrico)

    assert documento["keys"] == []


# ── Principal de sistema ──────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_no_se_emite_token_para_un_principal_de_sistema(emisor):
    """Un cron no tiene sesión ni identidad de usuario: usa `system_context()`, no un token."""
    cron = SystemPrincipal(name="cron:cerrar", scopes=frozenset({"register.close"}))
    contexto = AuthContext(actor=cron, subject=cron, transport="internal")

    with pytest.raises(TokenMalformedError, match="sistema"):
        await emisor.issue_access(contexto, session_id=uuid4())


# ── Basura ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "basura", ["", "no-es-un-token", "a.b", "....", "eyJhbGciOiJub25lIn0"]
)
@pytest.mark.anyio
async def test_un_token_corrupto_no_explota_con_500(verificador, basura):
    """
    Todo lo que no sea un token válido tiene que salir como `TokenMalformedError`, que mapea a
    401. Una excepción sin mapear sería un 500, y un 500 en el camino de auth es un canal de
    información además de una caída.
    """
    from hexcore.darwin import IdentityError

    with pytest.raises(IdentityError):
        await verificador.verify(basura, transport="cookie")
