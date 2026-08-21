"""
Darwin Fase 9: `impersonate`, contra SQLite y la app real.

Es el plugin que justifica que `AuthContext` tenga dos principales, así que este archivo prueba lo
que esa decisión hace posible — y sobre todo lo que hace **imposible**.

Lo adversarial que se fija:

- **No hay cadenas.** Impersonar estando impersonando se rechaza: en cadena, la auditoría
  apuntaría al intermedio, que nunca hizo nada.
- **No se impersona a quien también puede impersonar.** Sería escalada lateral con la traza
  borrada.
- **No se impersona a quien tiene un scope protegido.**
- **Una sesión impersonada NO se puede refrescar.** Es lo que hace real el techo de 60 minutos:
  con refresh, el techo sería "60 minutos por rotación", o sea ninguno.
- **Impersonar no presta permisos.** `has_scope` consulta al actor; el token lleva los scopes del
  operador, no los del sujeto.
- **La sesión del operador sobrevive.** Empezar no la toca, y terminar no la reconstruye.
- **Los dos principales quedan en la fila y en el token**, y `imp` viene en `True`.
- **Todo queda auditado con los dos**, incluido el inicio.
- **Un principal de sistema no puede impersonar**: no hay a quién responsabilizar.
- **El contexto impersonado cruza la cola** por el sobre firmado de la Fase 6.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    AuthContext,
    FixedClock,
    IdentityConfig,
    ImpersonationNotPermittedError,
    InvalidCredentialsError,
    PluginRegistry,
    Principal,
    StaticKeyStore,
    SystemPrincipal,
    TokenConfig,
    UnauthenticatedError,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    reset_identity,
)
from hexcore.darwin.application.services import IMPERSONATION_CAP  # noqa: E402
from hexcore.darwin.plugins.impersonate import (  # noqa: E402
    IMPERSONATE_SCOPE,
    ImpersonatePlugin,
    ImpersonationChainError,
    ImpersonationDeniedError,
    ImpersonationError,
    ImpersonationNotActiveError,
    ImpersonationSelfError,
    ImpersonationTargetProtectedError,
    ScopeImpersonationPolicy,
    get_impersonation_service,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48
PASS = "una frase larga y buena"
OPERADOR = "soporte@ejemplo.com"
CLIENTE = "cliente@ejemplo.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def plugin() -> ImpersonatePlugin:
    return ImpersonatePlugin(
        policy=ScopeImpersonationPolicy(protected_scopes=("admin",))
    )


@pytest.fixture
def sink():
    """Un sink de auditoría que guarda en memoria: la auditoría es la mitad del plugin."""
    from hexcore.darwin.domain.ports import AbstractAuditSink

    class EnMemoria(AbstractAuditSink):
        def __init__(self) -> None:
            self.registros: list[dict] = []

        async def record(self, **kwargs) -> None:
            self.registros.append(kwargs)

    return EnMemoria()


@pytest.fixture
def contenedor(reloj, plugin, sink):
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))

    from hexcore.config import LazyConfig
    from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

    LazyConfig.get_config().cache_backend = MemoryCache()

    reset_identity()
    plugin.reset()
    contenedor = configure_identity(
        IdentityConfig(
            secret_key=CLAVE,
            tokens=TokenConfig(issuer="https://api.test"),
            require_verified_email=False,
        ),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
        audit=sink,
        plugins=PluginRegistry([plugin]),
    )
    yield contenedor

    reset_identity()
    plugin.reset()
    asyncio.run(dispose_engine())


@pytest.fixture
def servicio(contenedor):
    return get_impersonation_service()


async def _usuario(contenedor, email: str, *, scopes: tuple[str, ...] = ()):
    usuario, _ = await contenedor.identity_service().sign_up(
        email=email, password=PASS
    )
    return await contenedor.users().update(
        usuario.model_copy(
            update={"email_verified": True, "extra": {"scopes": list(scopes)}}
        )
    )


def _contexto(usuario, *, scopes: tuple[str, ...] = (), session_id=None):
    """Un `AuthContext` normal (no impersonado) para ese usuario."""
    from uuid import uuid4

    principal = Principal(
        user_id=usuario.id,
        session_id=session_id or uuid4(),
        email=usuario.email,
        scopes=frozenset(scopes),
    )
    return AuthContext(actor=principal, subject=principal, transport="bearer")


# ── La política ───────────────────────────────────────────────────────────────
class TestPolitica:
    @pytest.mark.anyio
    async def test_sin_el_scope_se_rechaza(self, contenedor, servicio):
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        with pytest.raises(ImpersonationDeniedError, match=IMPERSONATE_SCOPE):
            await servicio.start(
                context=_contexto(operador),
                subject_id=cliente.id,
                reason="ticket #1",
            )

    @pytest.mark.anyio
    async def test_con_el_scope_se_permite(self, contenedor, servicio):
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
        )

        assert resultado.subject.id == cliente.id

    @pytest.mark.anyio
    async def test_no_se_impersona_a_uno_mismo(self, contenedor, servicio):
        """
        Produciría una sesión impersonada con actor == subject, que es exactamente el estado que
        el validador de `AuthContext` prohíbe. El error acá es claro; el de más adelante sería un
        `ValueError` del modelo.
        """
        operador = await _usuario(contenedor, OPERADOR)

        with pytest.raises(ImpersonationSelfError):
            await servicio.start(
                context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
                subject_id=operador.id,
                reason="ticket #1",
            )

    @pytest.mark.anyio
    async def test_no_se_impersona_en_cadena(self, contenedor, servicio):
        """
        ⚠️ La puerta más importante. Si A impersona a B y desde ahí impersona a C, la auditoría de
        la segunda dice que el actor es B — que nunca hizo nada. Es la forma más barata de borrar
        la traza.
        """
        operador = await _usuario(contenedor, OPERADOR, scopes=(IMPERSONATE_SCOPE,))
        cliente = await _usuario(contenedor, CLIENTE)
        tercero = await _usuario(contenedor, "otro@ejemplo.com")

        primera = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
            transport="bearer",
        )
        contexto_impersonado = await contenedor.session_service().authenticate(
            primera.tokens.access_token, transport="bearer"
        )

        with pytest.raises(ImpersonationChainError):
            await servicio.start(
                context=contexto_impersonado,
                subject_id=tercero.id,
                reason="ticket #2",
            )

    @pytest.mark.anyio
    async def test_no_se_impersona_a_otro_impersonador(self, contenedor, servicio):
        """Escalada lateral con la auditoría apuntando a la persona equivocada."""
        operador = await _usuario(contenedor, OPERADOR)
        otro_operador = await _usuario(
            contenedor, "soporte2@ejemplo.com", scopes=(IMPERSONATE_SCOPE,)
        )

        with pytest.raises(ImpersonationTargetProtectedError, match="lateral"):
            await servicio.start(
                context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
                subject_id=otro_operador.id,
                reason="ticket #1",
            )

    @pytest.mark.anyio
    async def test_no_se_impersona_a_quien_tiene_un_scope_protegido(
        self, contenedor, servicio
    ):
        operador = await _usuario(contenedor, OPERADOR)
        jefe = await _usuario(contenedor, "cto@ejemplo.com", scopes=("admin",))

        with pytest.raises(ImpersonationTargetProtectedError, match="admin"):
            await servicio.start(
                context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
                subject_id=jefe.id,
                reason="ticket #1",
            )

    @pytest.mark.anyio
    async def test_la_proteccion_de_impersonadores_se_puede_apagar(
        self, contenedor, reloj, sink
    ):
        """Existe la opción, y el test documenta qué se pierde al apagarla."""
        plugin = ImpersonatePlugin(
            policy=ScopeImpersonationPolicy(protect_impersonators=False)
        )
        reset_identity()
        cont = configure_identity(
            IdentityConfig(secret_key=CLAVE, require_verified_email=False),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            audit=sink,
            plugins=PluginRegistry([plugin]),
        )
        try:
            operador = await _usuario(cont, OPERADOR)
            otro = await _usuario(
                cont, "soporte2@ejemplo.com", scopes=(IMPERSONATE_SCOPE,)
            )

            resultado = await plugin.service().start(
                context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
                subject_id=otro.id,
                reason="ticket #1",
            )
            assert resultado.subject.id == otro.id
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_una_politica_propia_se_respeta(self, contenedor, reloj, sink):
        """El punto de extensión principal: un scope no responde "¿a *esta* persona?"."""
        from hexcore.darwin.plugins.impersonate import AbstractImpersonationPolicy

        class SoloDeMiEmpresa(AbstractImpersonationPolicy):
            def authorize(self, *, context, subject):
                if not subject.email.endswith("@ejemplo.com"):
                    raise ImpersonationDeniedError("Sólo dentro de la empresa.")

        plugin = ImpersonatePlugin(policy=SoloDeMiEmpresa())
        reset_identity()
        cont = configure_identity(
            IdentityConfig(secret_key=CLAVE, require_verified_email=False),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            audit=sink,
            plugins=PluginRegistry([plugin]),
        )
        try:
            operador = await _usuario(cont, OPERADOR)
            afuera = await _usuario(cont, "alguien@otraempresa.test")

            with pytest.raises(ImpersonationDeniedError, match="empresa"):
                await plugin.service().start(
                    context=_contexto(operador),
                    subject_id=afuera.id,
                    reason="ticket #1",
                )
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_un_extra_mal_formado_no_da_500(self, contenedor, servicio):
        """
        "Vacío" falla cerrando: un `extra` con basura no debería convertirse en un 500 en el
        camino de la autorización, ni en un pase libre.
        """
        operador = await _usuario(contenedor, OPERADOR)
        cliente, _ = await contenedor.identity_service().sign_up(
            email=CLIENTE, password=PASS
        )
        await contenedor.users().update(
            cliente.model_copy(update={"extra": {"scopes": "no-es-una-lista"}})
        )

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
        )
        assert resultado.subject.id == cliente.id


# ── Los requisitos previos ────────────────────────────────────────────────────
class TestRequisitos:
    @pytest.mark.anyio
    async def test_sin_contexto_no_se_puede(self, contenedor, servicio):
        """
        `None` es un error y no un caso anónimo: una impersonación sin actor conocido es
        justamente lo que no puede existir.
        """
        cliente = await _usuario(contenedor, CLIENTE)

        with pytest.raises(UnauthenticatedError):
            await servicio.start(
                context=None, subject_id=cliente.id, reason="ticket #1"
            )

    @pytest.mark.anyio
    async def test_el_motivo_es_obligatorio(self, contenedor, servicio):
        """
        Una auditoría que dice quién y a quién pero no por qué no responde la pregunta que se le
        hace seis meses después.
        """
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        for vacio in ("", "   ", "\t\n"):
            with pytest.raises(ValueError, match="motivo"):
                await servicio.start(
                    context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
                    subject_id=cliente.id,
                    reason=vacio,
                )

    @pytest.mark.anyio
    async def test_un_sujeto_inexistente_da_el_error_generico(self, contenedor, servicio):
        """
        Mismo error que un login fallido: un 404 distinto le confirmaría a un operador con
        permiso parcial qué ids existen.
        """
        from uuid import uuid4

        operador = await _usuario(contenedor, OPERADOR)

        with pytest.raises(InvalidCredentialsError):
            await servicio.start(
                context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
                subject_id=uuid4(),
                reason="ticket #1",
            )

    @pytest.mark.anyio
    async def test_un_principal_de_sistema_no_puede_impersonar(self, contenedor, servicio):
        """
        No hay a quién responsabilizar del acceso: `"cron:cerrar-registros"` no es una persona y
        no tiene fila en `user`.
        """
        cliente = await _usuario(contenedor, CLIENTE)
        principal = SystemPrincipal(
            name="cron:cerrar-registros", scopes=frozenset({IMPERSONATE_SCOPE})
        )
        contexto = AuthContext(
            actor=principal, subject=principal, transport="worker"
        )

        with pytest.raises(UnauthenticatedError, match="principal de sistema"):
            await servicio.start(
                context=contexto, subject_id=cliente.id, reason="ticket #1"
            )


# ── La sesión impersonada ─────────────────────────────────────────────────────
class TestSesionImpersonada:
    @pytest.mark.anyio
    async def test_la_fila_lleva_los_dos_principales(self, contenedor, servicio):
        """
        Lo que hace auditable a la impersonación: los dos ids **persistidos**, no un flag.
        """
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #4821",
        )

        sesion = resultado.session
        assert sesion.actor_user_id == operador.id
        assert sesion.subject_user_id == cliente.id
        assert sesion.impersonation_reason == "ticket #4821"
        assert sesion.impersonation_granted_by == operador.id
        assert sesion.is_impersonated is True

    @pytest.mark.anyio
    async def test_el_techo_es_de_una_hora(self, contenedor, servicio):
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
        )

        assert resultado.session.impersonation_expires_at == AHORA + IMPERSONATION_CAP
        assert IMPERSONATION_CAP == timedelta(minutes=60)

    @pytest.mark.anyio
    async def test_el_token_lleva_los_dos_y_el_flag(self, contenedor, servicio):
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
            transport="bearer",
        )

        claims = await contenedor.verifier().verify(
            resultado.tokens.access_token, transport="bearer"
        )

        assert claims.act == operador.id, "el actor es el operador"
        assert claims.sub == cliente.id, "el subject es el cliente"
        assert claims.imp is True

    @pytest.mark.anyio
    async def test_impersonar_no_presta_permisos(self, contenedor, servicio):
        """
        ⚠️ El operador ve lo que el otro ve, y puede hacer lo que **él** puede hacer. Los scopes
        del token son los del actor: si fueran los del subject, impersonar a un admin daría los
        permisos del admin — y la política que protege a los admins sería la única defensa.
        """
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE, scopes=("secreto:leer",))

        resultado = await servicio.start(
            context=_contexto(
                operador, scopes=(IMPERSONATE_SCOPE, "soporte:leer")
            ),
            subject_id=cliente.id,
            reason="ticket #1",
            transport="bearer",
        )
        claims = await contenedor.verifier().verify(
            resultado.tokens.access_token, transport="bearer"
        )

        assert "soporte:leer" in claims.scopes
        assert "secreto:leer" not in claims.scopes

    @pytest.mark.anyio
    async def test_el_contexto_reconstruido_consulta_al_actor(self, contenedor, servicio):
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(
                operador, scopes=(IMPERSONATE_SCOPE, "soporte:leer")
            ),
            subject_id=cliente.id,
            reason="ticket #1",
            transport="bearer",
        )
        contexto = await contenedor.session_service().authenticate(
            resultado.tokens.access_token, transport="bearer"
        )

        assert contexto.is_impersonating is True
        assert contexto.has_scope("soporte:leer") is True
        assert contexto.actor_id == operador.id
        assert contexto.subject_id == cliente.id

    @pytest.mark.anyio
    async def test_la_sesion_del_operador_sobrevive(self, contenedor, servicio):
        """
        Empezar no la toca, así que terminar es descartar el token de impersonación. Sin esta
        propiedad, "volver" sería un segundo intercambio que puede fallar a mitad de camino.
        """
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        _, propia, par_propio = await contenedor.identity_service().sign_in(
            email=OPERADOR, password=PASS, transport="bearer",
            scopes=(IMPERSONATE_SCOPE,),
        )
        contexto = await contenedor.session_service().authenticate(
            par_propio.access_token, transport="bearer"
        )

        await servicio.start(
            context=contexto, subject_id=cliente.id, reason="ticket #1",
            transport="bearer",
        )

        # La sesión del operador sigue vigente: su token sigue verificando y su fila no está
        # revocada.
        assert await contenedor.verifier().verify(
            par_propio.access_token, transport="bearer"
        )
        fila = await contenedor.sessions_repository().get(propia.id)
        assert fila is not None and fila.revoked_at is None
        assert fila.actor_user_id == operador.id
        assert fila.subject_user_id == operador.id, "su sesión no quedó impersonada"


# ── El refresh, que es lo que hace real el techo ──────────────────────────────
class TestRefresh:
    @pytest.mark.anyio
    async def test_una_sesion_impersonada_no_se_refresca(self, contenedor, servicio):
        """
        ⚠️ Es el mecanismo que hace que el techo de 60 minutos sea real. Si se pudiera refrescar,
        el operador extendería la sesión indefinidamente sin volver a pedir permiso ni dejar un
        segundo registro de auditoría: el techo pasaría a ser "60 minutos por refresh".
        """
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
            transport="bearer",
        )

        with pytest.raises(ImpersonationNotPermittedError, match="no se puede refrescar"):
            await contenedor.session_service().refresh(
                resultado.tokens.refresh_token, transport="bearer"
            )

    @pytest.mark.anyio
    async def test_el_rechazo_no_consume_la_fila(self, contenedor, servicio):
        """
        Se chequea **antes** de `consume_for_rotation`: consumir la fila y después rechazar
        dejaría la sesión inutilizable por lo que queda de su hora.
        """
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
            transport="bearer",
        )

        with pytest.raises(ImpersonationNotPermittedError):
            await contenedor.session_service().refresh(
                resultado.tokens.refresh_token, transport="bearer"
            )

        fila = await contenedor.sessions_repository().get(resultado.session.id)
        assert fila is not None
        assert fila.consumed_at is None, "la fila quedó usable"
        assert fila.revoked_at is None, "y no se disparó la detección de reuso"

        # Y el access token sigue sirviendo por lo que le queda.
        assert await contenedor.verifier().verify(
            resultado.tokens.access_token, transport="bearer"
        )

    @pytest.mark.anyio
    async def test_una_sesion_normal_si_se_refresca(self, contenedor):
        """El chequeo no rompe el camino normal."""
        await _usuario(contenedor, OPERADOR)
        _, _, par = await contenedor.identity_service().sign_in(
            email=OPERADOR, password=PASS, transport="bearer"
        )

        _, nuevo = await contenedor.session_service().refresh(
            par.refresh_token, transport="bearer"
        )

        assert nuevo.access_token


# ── Terminar ──────────────────────────────────────────────────────────────────
class TestStop:
    @pytest.mark.anyio
    async def test_terminar_revoca_la_sesion_impersonada(self, contenedor, servicio):
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
            transport="bearer",
        )
        contexto = await contenedor.session_service().authenticate(
            resultado.tokens.access_token, transport="bearer"
        )

        await servicio.stop(context=contexto)

        fila = await contenedor.sessions_repository().get(resultado.session.id)
        assert fila is not None and fila.revoked_at is not None

    @pytest.mark.anyio
    async def test_terminar_una_sesion_normal_es_un_409(self, contenedor, servicio):
        """
        Se distingue de "no autorizado": es un error del cliente que llamó al endpoint
        equivocado, no un intento de escalada, y confundirlos llena la auditoría de falsos
        positivos.
        """
        operador = await _usuario(contenedor, OPERADOR)

        with pytest.raises(ImpersonationNotActiveError):
            await servicio.stop(context=_contexto(operador))

    @pytest.mark.anyio
    async def test_terminar_sin_contexto_falla(self, servicio):
        with pytest.raises(UnauthenticatedError):
            await servicio.stop(context=None)


# ── La auditoría ──────────────────────────────────────────────────────────────
class TestAuditoria:
    @pytest.mark.anyio
    async def test_el_inicio_queda_registrado_con_los_dos(
        self, contenedor, servicio, sink
    ):
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #4821",
        )

        registro = next(
            r for r in sink.registros if r["action"] == "impersonation.start"
        )
        assert registro["actor_id"] == operador.id
        assert registro["subject_id"] == cliente.id
        assert registro["impersonated"] is True
        assert registro["metadata"]["reason"] == "ticket #4821"
        assert registro["metadata"]["expires_at"]

    @pytest.mark.anyio
    async def test_el_registro_lleva_la_sesion_del_operador(
        self, contenedor, servicio, sink
    ):
        """
        Para poder correlacionar: sin el `operator_session_id`, una investigación no puede unir
        la impersonación con lo que el operador hizo desde su propia sesión.
        """
        from uuid import uuid4

        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)
        sid = uuid4()

        await servicio.start(
            context=_contexto(
                operador, scopes=(IMPERSONATE_SCOPE,), session_id=sid
            ),
            subject_id=cliente.id,
            reason="ticket #1",
        )

        registro = next(
            r for r in sink.registros if r["action"] == "impersonation.start"
        )
        assert registro["metadata"]["operator_session_id"] == str(sid)

    @pytest.mark.anyio
    async def test_el_fin_queda_registrado(self, contenedor, servicio, sink):
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #1",
            transport="bearer",
        )
        contexto = await contenedor.session_service().authenticate(
            resultado.tokens.access_token, transport="bearer"
        )

        await servicio.stop(context=contexto)

        registro = next(
            r for r in sink.registros if r["action"] == "impersonation.stop"
        )
        assert registro["actor_id"] == operador.id
        assert registro["subject_id"] == cliente.id
        assert registro["impersonated"] is True

    @pytest.mark.anyio
    async def test_un_rechazo_no_deja_sesion(self, contenedor, servicio):
        """
        La política decide **antes** de que exista cualquier sesión: si autorizara después, un
        rechazo dejaría una sesión impersonada huérfana que hay que revocar — y el camino de
        limpieza es el que falla.
        """
        from sqlalchemy import func, select

        from hexcore.darwin.infrastructure.models import SessionModel
        from hexcore.infrastructure.uow.scopes import session_scope

        operador = await _usuario(contenedor, OPERADOR)
        jefe = await _usuario(contenedor, "cto@ejemplo.com", scopes=("admin",))

        async with session_scope() as sesion:
            antes = int(
                (
                    await sesion.execute(select(func.count()).select_from(SessionModel))
                ).scalar_one()
            )

        with pytest.raises(ImpersonationTargetProtectedError):
            await servicio.start(
                context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
                subject_id=jefe.id,
                reason="ticket #1",
            )

        async with session_scope() as sesion:
            despues = int(
                (
                    await sesion.execute(select(func.count()).select_from(SessionModel))
                ).scalar_one()
            )
        assert despues == antes


# ── Describir ─────────────────────────────────────────────────────────────────
class TestDescribe:
    @pytest.mark.anyio
    async def test_sin_contexto_no_esta_activa(self, servicio):
        assert (await servicio.describe(None)).active is False

    @pytest.mark.anyio
    async def test_una_sesion_normal_no_esta_activa(self, contenedor, servicio):
        operador = await _usuario(contenedor, OPERADOR)

        assert (await servicio.describe(_contexto(operador))).active is False

    @pytest.mark.anyio
    async def test_impersonando_trae_todo(self, contenedor, servicio):
        """
        Es lo que alimenta la barra de "estás viendo como…", y esa barra no es cosmética: sin
        ella un operador olvida que está impersonando y decide creyendo que actúa como él mismo.
        """
        operador = await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)

        resultado = await servicio.start(
            context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
            subject_id=cliente.id,
            reason="ticket #4821",
            transport="bearer",
        )
        contexto = await contenedor.session_service().authenticate(
            resultado.tokens.access_token, transport="bearer"
        )

        info = await servicio.describe(contexto)

        assert info.active is True
        assert info.actor_id == operador.id
        assert info.subject_id == cliente.id
        assert info.expires_at == AHORA + IMPERSONATION_CAP


# ── El sobre que cruza la cola (Fase 6) ───────────────────────────────────────
@pytest.mark.anyio
async def test_el_contexto_impersonado_cruza_la_cola(contenedor, servicio):
    """
    La razón por la que la Fase 6 es prerrequisito de esta. Un comando encolado durante una
    impersonación tiene que procesarse en el worker con **los dos** principales: si sólo viajara
    el subject, la auditoría del worker diría que la acción la hizo el cliente.
    """
    from hexcore.darwin.domain.context import auth_scope, current_auth

    operador = await _usuario(contenedor, OPERADOR)
    cliente = await _usuario(contenedor, CLIENTE)

    resultado = await servicio.start(
        context=_contexto(operador, scopes=(IMPERSONATE_SCOPE,)),
        subject_id=cliente.id,
        reason="ticket #1",
        transport="bearer",
    )
    contexto = await contenedor.session_service().authenticate(
        resultado.tokens.access_token, transport="bearer"
    )

    from hexcore.domain.cqrs.commands import Command

    class CerrarTicket(Command):
        ticket: str = "4821"

    mensaje = CerrarTicket()
    codec = contenedor.envelope_codec()
    restorer = contenedor.envelope_restorer()

    with auth_scope(contexto):
        actual = current_auth()
        assert actual is not None
        sobre = codec.seal(actual, mensaje)

    # Afuera del scope, el contexto ambiental ya no está: el sobre es lo único que cruza.
    assert current_auth() is None

    async with restorer.restore(sobre, mensaje):
        en_el_worker = current_auth()
        assert en_el_worker is not None
        assert en_el_worker.is_impersonating is True
        assert en_el_worker.actor_id == operador.id
        assert en_el_worker.subject_id == cliente.id
        assert en_el_worker.transport == "worker", (
            "un job de background no está sirviendo un request con cookie"
        )

    # Y el sobre está atado al mensaje: re-adjuntarlo a otro no verifica.
    class TransferirFondos(Command):
        monto: int = 1_000_000

    with pytest.raises(Exception):
        codec.open(sobre, TransferirFondos())


# ── El plugin como plugin ─────────────────────────────────────────────────────
class TestPlugin:
    def test_no_aporta_ninguna_tabla(self, plugin):
        """
        La impersonación está modelada en `session` desde la Fase 3, justamente para que este
        plugin no tenga que inventar nada.
        """
        assert plugin.tables() == {}

    def test_aporta_su_mapa_de_excepciones(self, plugin):
        mapa = plugin.exception_status_map()

        assert mapa[ImpersonationDeniedError] == 403
        assert mapa[ImpersonationChainError] == 403
        assert mapa[ImpersonationSelfError] == 409
        assert mapa[ImpersonationNotActiveError] == 409

    def test_no_mapea_la_excepcion_base(self, plugin):
        assert ImpersonationError not in plugin.exception_status_map()

    def test_avisa_si_no_hay_auditoria(self, reloj, caplog):
        """
        Una impersonación sin auditoría es exactamente lo que el plugin promete que no pasa, así
        que el aviso va en el arranque y no en un docstring.
        """
        import logging

        plugin = ImpersonatePlugin()
        reset_identity()
        configure_identity(
            IdentityConfig(secret_key=CLAVE),
            clock=reloj,
            key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
            plugins=PluginRegistry([plugin]),
        )
        try:
            with caplog.at_level(
                logging.WARNING, logger="hexcore.darwin.impersonate"
            ):
                asyncio.run(plugin.startup_steps()[0]())

            assert "no va a quedar registrada" in caplog.text
        finally:
            reset_identity()

    def test_calla_con_auditoria(self, contenedor, plugin, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="hexcore.darwin.impersonate"):
            asyncio.run(plugin.startup_steps()[0]())

        assert caplog.text == ""

    def test_registra_sus_handlers(self, plugin):
        from hexcore.application.cqrs.registry import HandlerRegistry
        from hexcore.darwin.plugins.impersonate.commands import (
            StartImpersonation,
            StopImpersonation,
        )

        registro = HandlerRegistry()
        plugin.register_handlers(registro)

        assert registro.resolve_command_handler(StartImpersonation) is not None
        assert registro.resolve_command_handler(StopImpersonation) is not None

    def test_los_comandos_no_llevan_el_actor(self):
        """
        Un campo que el llamador rellena es un campo que el llamador puede mentir, y acá mentirlo
        sería impersonar en nombre de otro.
        """
        from hexcore.darwin.plugins.impersonate.commands import StartImpersonation

        campos = set(StartImpersonation.model_fields)

        assert "actor_id" not in campos
        assert "subject_id" in campos and "reason" in campos

    def test_convive_con_los_otros_dos_plugins(self):
        from hexcore.darwin.plugins.oauth import OAuthPlugin
        from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

        registro = PluginRegistry(
            [ImpersonatePlugin(), OAuthPlugin(), TwoFactorPlugin()]
        )
        registro.validate()

        assert registro.names == ("two_factor", "oauth", "impersonate")


# ── El borde HTTP ─────────────────────────────────────────────────────────────
@pytest.fixture
def cliente_http(contenedor, plugin):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hexcore.darwin import build_identity_router
    from hexcore.fastapi import AppFeatures, create_app

    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True, health=False),
        routers=[build_identity_router(), *plugin.routers()],
    )
    with TestClient(app) as http:
        yield http


async def _bearer(contenedor, email, scopes=()):
    _, _, par = await contenedor.identity_service().sign_in(
        email=email, password=PASS, transport="bearer", scopes=scopes
    )
    return {
        "Authorization": f"Bearer {par.access_token}",
        "X-Darwin-Transport": "bearer",
    }


class TestHttp:
    @pytest.mark.anyio
    async def test_el_flujo_completo(self, contenedor, cliente_http):
        await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)
        auth = await _bearer(contenedor, OPERADOR, (IMPERSONATE_SCOPE,))

        estado = cliente_http.get("/auth/impersonate", headers=auth)
        assert estado.json()["active"] is False

        inicio = cliente_http.post(
            f"/auth/impersonate/{cliente.id}",
            json={"reason": "ticket #4821"},
            headers=auth,
        )
        assert inicio.status_code == 200, inicio.text
        cuerpo = inicio.json()
        assert cuerpo["impersonating"] == str(cliente.id)
        assert cuerpo["expires_at"]

        como_cliente = {
            "Authorization": f"Bearer {cuerpo['access_token']}",
            "X-Darwin-Transport": "bearer",
        }
        estado = cliente_http.get("/auth/impersonate", headers=como_cliente).json()
        assert estado["active"] is True
        assert estado["reason"] == "ticket #4821"

        fin = cliente_http.post("/auth/impersonate/stop", headers=como_cliente)
        assert fin.status_code == 200
        assert fin.json() == {"stopped": True}

    @pytest.mark.anyio
    async def test_sin_el_scope_da_403(self, contenedor, cliente_http):
        await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)
        auth = await _bearer(contenedor, OPERADOR)

        respuesta = cliente_http.post(
            f"/auth/impersonate/{cliente.id}",
            json={"reason": "ticket #1"},
            headers=auth,
        )

        assert respuesta.status_code == 403, respuesta.text

    @pytest.mark.anyio
    async def test_sin_sesion_da_401(self, contenedor, cliente_http):
        cliente = await _usuario(contenedor, CLIENTE)

        respuesta = cliente_http.post(
            f"/auth/impersonate/{cliente.id}", json={"reason": "x"}
        )

        assert respuesta.status_code == 401

    @pytest.mark.anyio
    async def test_un_motivo_vacio_da_422(self, contenedor, cliente_http):
        """El 422 sale del borde y no del dominio: dice qué campo falta."""
        await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)
        auth = await _bearer(contenedor, OPERADOR, (IMPERSONATE_SCOPE,))

        respuesta = cliente_http.post(
            f"/auth/impersonate/{cliente.id}", json={"reason": ""}, headers=auth
        )

        assert respuesta.status_code == 422

    @pytest.mark.anyio
    async def test_impersonarse_a_uno_mismo_da_409(self, contenedor, cliente_http):
        operador = await _usuario(contenedor, OPERADOR)
        auth = await _bearer(contenedor, OPERADOR, (IMPERSONATE_SCOPE,))

        respuesta = cliente_http.post(
            f"/auth/impersonate/{operador.id}",
            json={"reason": "ticket #1"},
            headers=auth,
        )

        assert respuesta.status_code == 409, respuesta.text

    @pytest.mark.anyio
    async def test_terminar_una_sesion_normal_da_409(self, contenedor, cliente_http):
        await _usuario(contenedor, OPERADOR)
        auth = await _bearer(contenedor, OPERADOR, (IMPERSONATE_SCOPE,))

        respuesta = cliente_http.post("/auth/impersonate/stop", headers=auth)

        assert respuesta.status_code == 409, respuesta.text

    @pytest.mark.anyio
    async def test_refrescar_impersonando_da_403(self, contenedor, cliente_http):
        """El techo de 60 minutos, visto desde el borde."""
        await _usuario(contenedor, OPERADOR)
        cliente = await _usuario(contenedor, CLIENTE)
        auth = await _bearer(contenedor, OPERADOR, (IMPERSONATE_SCOPE,))

        inicio = cliente_http.post(
            f"/auth/impersonate/{cliente.id}",
            json={"reason": "ticket #1"},
            headers=auth,
        ).json()

        respuesta = cliente_http.post(
            "/auth/refresh",
            headers={
                "X-Darwin-Transport": "bearer",
                "X-Refresh-Token": inicio["refresh_token"],
            },
        )

        assert respuesta.status_code == 403, respuesta.text
