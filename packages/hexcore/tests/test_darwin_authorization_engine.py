"""
Darwin — Fase 0 del plan rbac/drbac: el contrato de autorización en el núcleo.

Lo que se fija acá:

1. `ScopeAuthorizationProvider` respeta los comodines de `Permission.grants`, que es
   exactamente la asimetría que `AuthContext.has_scope` (match exacto) no resuelve.
2. `AuthorizationEngine` combina con **deny-overrides + default-deny**: cualquier `deny` gana,
   `not_applicable` en todos —incluido el motor sin providers— deniega, y un provider que
   explota se trata como `deny` sin tumbar la request.
3. `authorize()` usa el actor en curso (`require_auth()`) y lanza `AccessDeniedError` con
   `required` = la acción pedida.
4. El punto de extensión de `DarwinPlugin` (`authorization_providers`) es concreto y vacío por
   default, y `PluginRegistry` los agrega en orden de plugin.
5. Una app sin plugins de autorización se comporta igual que antes de este módulo: el único
   camino es el scope retrocompatible.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from hexcore.darwin import (
    AccessDeniedError,
    AccessRequest,
    AuthContext,
    AuthorizationEngine,
    AuthorizationProvider,
    Decision,
    DarwinPlugin,
    Principal,
    ResourceRef,
    ScopeAuthorizationProvider,
    UnauthenticatedError,
    auth_scope,
    require_auth,
)
from hexcore.darwin.application.plugins import PluginRegistry

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _actor(*, scopes: frozenset[str] = frozenset()) -> Principal:
    return Principal(user_id=uuid4(), email="ana@test.com", scopes=scopes)


def _contexto(actor: Principal) -> AuthContext[t.Any]:
    return AuthContext(actor=actor, subject=actor, transport="cookie")


def _request(action: str, *, scopes: frozenset[str] = frozenset()) -> AccessRequest:
    return AccessRequest(context=_contexto(_actor(scopes=scopes)), action=action)


class SiempreDecide(AuthorizationProvider):
    """Un provider de prueba que siempre devuelve el efecto declarado."""

    name = "siempre"

    def __init__(self, effect: str) -> None:
        self._effect = effect

    async def decide(self, request: AccessRequest) -> Decision:
        del request
        return Decision(effect=self._effect, provider=self.name)  # type: ignore[arg-type]


class Explota(AuthorizationProvider):
    name = "roto"

    async def decide(self, request: AccessRequest) -> Decision:
        del request
        raise RuntimeError("el provider está roto")


# ── ScopeAuthorizationProvider ────────────────────────────────────────────────
class TestScopeAuthorizationProvider:
    @pytest.mark.anyio
    async def test_scope_exacto_concede(self):
        provider = ScopeAuthorizationProvider()
        decision = await provider.decide(_request("users.invite", scopes=frozenset({"users.invite"})))

        assert decision.effect == "allow"
        assert decision.provider == "scope"

    @pytest.mark.anyio
    async def test_comodin_concede_lo_que_promete(self):
        """
        La asimetría que este provider arregla: `AuthContext.has_scope` hace un match exacto
        y `"users.invite"` no pasaría contra un scope `"users.*"`. Acá sí, vía
        `Permission.grants`.
        """
        provider = ScopeAuthorizationProvider()
        decision = await provider.decide(_request("users.invite", scopes=frozenset({"users.*"})))

        assert decision.effect == "allow"

    @pytest.mark.anyio
    async def test_comodin_no_concede_el_nodo_pelado(self):
        """`users.*` concede descendientes, no `users` pelado — misma semántica que `Permission`."""
        provider = ScopeAuthorizationProvider()
        decision = await provider.decide(_request("users", scopes=frozenset({"users.*"})))

        assert decision.effect == "not_applicable"

    @pytest.mark.anyio
    async def test_sin_el_scope_no_aplica(self):
        """`not_applicable`, no `deny`: este provider no tiene autoridad para negar nada."""
        provider = ScopeAuthorizationProvider()
        decision = await provider.decide(_request("users.invite", scopes=frozenset({"invoice.read"})))

        assert decision.effect == "not_applicable"

    @pytest.mark.anyio
    async def test_siempre_evalua_al_actor_no_al_sujeto(self):
        """
        Misma regla que `AuthContext.has_scope`: en una impersonación, lo que se puede hacer
        lo determina quien ejecuta, nunca la cuenta afectada.
        """
        from hexcore.darwin import Impersonation

        soporte = _actor(scopes=frozenset({"users.*"}))
        cliente = _actor(scopes=frozenset())
        ctx = AuthContext(
            actor=soporte,
            subject=cliente,
            transport="cookie",
            impersonation=Impersonation(
                granted_by=uuid4(),
                reason="ticket #1",
                granted_at=AHORA,
                expires_at=AHORA.replace(hour=13),
            ),
        )

        decision = await ScopeAuthorizationProvider().decide(
            AccessRequest(context=ctx, action="users.invite")
        )

        assert decision.effect == "allow"


# ── AuthorizationEngine: combinación ──────────────────────────────────────────
class TestCombinacion:
    @pytest.mark.anyio
    async def test_sin_providers_deniega(self):
        """Fail-closed: cero providers es el caso límite de 'todos dijeron not_applicable'."""
        engine = AuthorizationEngine([])

        decision = await engine.decide(_request("invoice.approve"))

        assert decision.effect == "deny"
        assert decision.allowed is False

    @pytest.mark.anyio
    async def test_todos_not_applicable_deniega(self):
        engine = AuthorizationEngine([SiempreDecide("not_applicable"), SiempreDecide("not_applicable")])

        decision = await engine.decide(_request("invoice.approve"))

        assert decision.effect == "deny"

    @pytest.mark.anyio
    async def test_un_allow_sin_deny_permite(self):
        engine = AuthorizationEngine([SiempreDecide("not_applicable"), SiempreDecide("allow")])

        decision = await engine.decide(_request("invoice.approve"))

        assert decision.allowed is True

    @pytest.mark.anyio
    async def test_deny_gana_sobre_allow(self):
        """
        El corazón de deny-overrides: es lo que permite que un plugin restrinja lo que otro
        concedió, sin importar el orden en que se registraron.
        """
        engine = AuthorizationEngine([SiempreDecide("allow"), SiempreDecide("deny")])

        decision = await engine.decide(_request("invoice.approve"))

        assert decision.effect == "deny"

    @pytest.mark.anyio
    async def test_deny_gana_sin_importar_el_orden(self):
        engine = AuthorizationEngine([SiempreDecide("deny"), SiempreDecide("allow")])

        decision = await engine.decide(_request("invoice.approve"))

        assert decision.effect == "deny"

    @pytest.mark.anyio
    async def test_un_provider_que_explota_se_trata_como_deny(self):
        """
        Fail-closed literal: un provider roto deniega, no tumba la request con un 500 ni se
        lee como si hubiera autorizado.
        """
        engine = AuthorizationEngine([Explota(), SiempreDecide("allow")])

        decision = await engine.decide(_request("invoice.approve"))

        assert decision.effect == "deny"
        assert decision.provider == "roto"

    @pytest.mark.anyio
    async def test_el_resto_de_los_providers_corre_igual_si_uno_explota(self):
        """Un provider roto no corta la evaluación de los demás."""
        visto: list[str] = []

        class Testigo(AuthorizationProvider):
            name = "testigo"

            async def decide(self, request: AccessRequest) -> Decision:
                visto.append(request.action)
                return Decision(effect="not_applicable", provider=self.name)

        engine = AuthorizationEngine([Explota(), Testigo()])
        await engine.decide(_request("invoice.approve"))

        assert visto == ["invoice.approve"]

    @pytest.mark.anyio
    async def test_scope_provider_de_verdad_participa_del_combinador(self):
        engine = AuthorizationEngine([ScopeAuthorizationProvider()])

        permitido = await engine.decide(_request("invoice.approve", scopes=frozenset({"invoice.*"})))
        denegado = await engine.decide(_request("invoice.approve", scopes=frozenset()))

        assert permitido.allowed is True
        assert denegado.allowed is False


# ── authorize(): la forma imperativa ──────────────────────────────────────────
class TestAuthorize:
    @pytest.mark.anyio
    async def test_sin_contexto_en_curso_lanza_unauthenticated(self):
        from hexcore.darwin.application.authorization import authorize

        with pytest.raises(UnauthenticatedError):
            await authorize("invoice.approve")

    @pytest.mark.anyio
    async def test_denegado_lanza_access_denied_con_la_accion_pedida(self):
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity
        from hexcore.darwin.application.authorization import authorize

        pytest.importorskip("joserfc")
        pytest.importorskip("argon2")

        reset_identity()
        try:
            configure_identity(IdentityConfig(storage="sqlalchemy", secret_key="k" * 48))
            with auth_scope(_contexto(_actor(scopes=frozenset()))):
                with pytest.raises(AccessDeniedError) as excinfo:
                    await authorize("invoice.approve")

            assert excinfo.value.required == "invoice.approve"
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_permitido_devuelve_la_decision(self):
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity
        from hexcore.darwin.application.authorization import authorize

        pytest.importorskip("joserfc")
        pytest.importorskip("argon2")

        reset_identity()
        try:
            configure_identity(IdentityConfig(storage="sqlalchemy", secret_key="k" * 48))
            with auth_scope(_contexto(_actor(scopes=frozenset({"invoice.approve"})))):
                decision = await authorize("invoice.approve", ResourceRef(type="invoice", id="1"))

            assert decision.allowed is True
        finally:
            reset_identity()


# ── El punto de extensión de los plugins ──────────────────────────────────────
class TestPuntoDeExtension:
    def test_authorization_providers_es_concreto_y_vacio_por_default(self):
        class Minimo(DarwinPlugin):
            name = "minimo"

        assert Minimo().authorization_providers() == ()

    def test_el_registro_agrega_los_providers_de_todos_los_plugins(self):
        provider_a = SiempreDecide("allow")
        provider_b = SiempreDecide("deny")

        class PluginA(DarwinPlugin):
            name = "a"

            def authorization_providers(self):
                return [provider_a]

        class PluginB(DarwinPlugin):
            name = "b"

            def authorization_providers(self):
                return [provider_b]

        registro = PluginRegistry([PluginA(), PluginB()])
        registro.validate()

        assert registro.authorization_providers() == [provider_a, provider_b]

    def test_sin_plugins_el_registro_no_aporta_nada(self):
        registro = PluginRegistry()
        registro.validate()

        assert registro.authorization_providers() == []


# ── El contenedor: comportamiento sin plugins ─────────────────────────────────
class TestContenedor:
    def test_sin_plugins_el_motor_es_solo_el_scope_retrocompatible(self):
        """
        DoD de la Fase 0: una app sin plugins de autorización se comporta igual que antes de
        este módulo.
        """
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

        pytest.importorskip("joserfc")
        pytest.importorskip("argon2")

        reset_identity()
        try:
            contenedor = configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key="k" * 48)
            )
            engine = contenedor.authorizer()

            assert len(engine._providers) == 1
            assert isinstance(engine._providers[0], ScopeAuthorizationProvider)
        finally:
            reset_identity()

    def test_el_motor_se_cachea(self):
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

        pytest.importorskip("joserfc")
        pytest.importorskip("argon2")

        reset_identity()
        try:
            contenedor = configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key="k" * 48)
            )
            assert contenedor.authorizer() is contenedor.authorizer()
        finally:
            reset_identity()

    def test_los_providers_de_los_plugins_van_antes_que_el_scope(self):
        """
        El orden importa para el motor que corta apenas encuentra el primer `deny`: un plugin
        tiene que poder restringir antes de que el scope retrocompatible ya haya opinado.
        """
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

        pytest.importorskip("joserfc")
        pytest.importorskip("argon2")

        provider = SiempreDecide("deny")

        class Plugin(DarwinPlugin):
            name = "restrictor"

            def authorization_providers(self):
                return [provider]

        reset_identity()
        try:
            contenedor = configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key="k" * 48),
                plugins=[Plugin()],
            )
            engine = contenedor.authorizer()

            assert engine._providers[0] is provider
            assert isinstance(engine._providers[-1], ScopeAuthorizationProvider)
        finally:
            reset_identity()
