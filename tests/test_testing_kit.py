"""
Fase 10: el kit de testing — el genérico y el de Darwin.

Un doble de prueba tiene un modo de falla propio y peor que el de cualquier otro código: **si es
más permisivo que la implementación, hace pasar tests que deberían fallar**. Este archivo prueba
sobre todo eso — que los fakes se comporten como lo real en los puntos donde la diferencia
importaría:

- **Guardan copias**, así que mutar la entidad después de guardarla no cambia lo guardado. Es el
  falso positivo más común de un repositorio en memoria.
- **El rollback deshace de verdad.** Un doble que sólo cuenta la llamada hace que los tests de
  transaccionalidad pasen sin probar nada.
- **Las operaciones atómicas siguen siendo de un solo paso**: de dos canjes concurrentes gana uno,
  igual que con el `UPDATE ... RETURNING` real.
- **`add` de un mail repetido lanza**, igual que el `UNIQUE`.
- **La denylist vence**, porque la real guarda el vencimiento adentro del valor.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hexcore.domain.base import BaseEntity
from hexcore.testing import FakeRepository, FakeUnitOfWork

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Cosa(BaseEntity):
    """Una entidad de prueba con un campo mutable adentro, para el test de aliasing."""

    nombre: str = "sin nombre"
    etiquetas: list[str] = []


# ── El repositorio genérico ───────────────────────────────────────────────────
class TestFakeRepository:
    @pytest.mark.anyio
    async def test_guarda_y_devuelve(self):
        repo: FakeRepository[Cosa] = FakeRepository()
        cosa = Cosa(nombre="una")

        guardada = await repo.save(cosa)

        assert guardada.id == cosa.id
        assert (await repo.get_by_id(cosa.id)).nombre == "una"

    @pytest.mark.anyio
    async def test_guarda_copias_y_no_referencias(self):
        """
        ⚠️ El falso positivo más común de un repositorio en memoria: mutar la entidad después de
        guardarla cambiaría lo guardado, y un test pasaría por una aliasing que en producción no
        existe — el repositorio real serializa a la base.
        """
        repo: FakeRepository[Cosa] = FakeRepository()
        cosa = Cosa(nombre="original")
        await repo.save(cosa)

        cosa.nombre = "mutada"

        assert (await repo.get_by_id(cosa.id)).nombre == "original"

    @pytest.mark.anyio
    async def test_la_copia_es_profunda(self):
        """
        Con una copia superficial, una lista adentro se seguiría compartiendo — y ese es el caso
        donde el aliasing pasa desapercibido.
        """
        repo: FakeRepository[Cosa] = FakeRepository()
        cosa = Cosa(etiquetas=["a"])
        await repo.save(cosa)

        cosa.etiquetas.append("b")

        assert (await repo.get_by_id(cosa.id)).etiquetas == ["a"]

    @pytest.mark.anyio
    async def test_lo_que_sale_tambien_es_copia(self):
        """En los dos sentidos: mutar lo devuelto no toca lo guardado."""
        repo: FakeRepository[Cosa] = FakeRepository()
        cosa = Cosa(nombre="original")
        await repo.save(cosa)

        leida = await repo.get_by_id(cosa.id)
        leida.nombre = "mutada"

        assert (await repo.get_by_id(cosa.id)).nombre == "original"

    @pytest.mark.anyio
    async def test_get_by_id_inexistente_lanza(self):
        """
        Lanza y no devuelve `None`: el test lo ve acá en vez de fallar tres líneas después con un
        `AttributeError` sobre `None`.
        """
        repo: FakeRepository[Cosa] = FakeRepository()

        with pytest.raises(KeyError):
            await repo.get_by_id(uuid4())

    @pytest.mark.anyio
    async def test_la_excepcion_es_configurable(self):
        class MiError(Exception):
            pass

        repo: FakeRepository[Cosa] = FakeRepository(raise_on_missing=MiError)

        with pytest.raises(MiError):
            await repo.get_by_id(uuid4())

    @pytest.mark.anyio
    async def test_list_all_pagina(self):
        cosas = [Cosa(nombre=f"c{i}") for i in range(5)]
        repo: FakeRepository[Cosa] = FakeRepository(entities=cosas)

        assert len(await repo.list_all()) == 5
        assert [c.nombre for c in await repo.list_all(limit=2)] == ["c0", "c1"]
        assert [c.nombre for c in await repo.list_all(limit=2, offset=3)] == ["c3", "c4"]

    @pytest.mark.anyio
    async def test_el_orden_es_de_insercion(self):
        """
        Determinista a propósito: con un `set` el orden dependería del hash y los tests de
        paginación fallarían una vez cada tanto.
        """
        cosas = [Cosa(nombre=f"c{i}") for i in range(20)]
        repo: FakeRepository[Cosa] = FakeRepository(entities=cosas)

        assert [c.nombre for c in await repo.list_all()] == [
            f"c{i}" for i in range(20)
        ]

    @pytest.mark.anyio
    async def test_delete(self):
        cosa = Cosa()
        repo: FakeRepository[Cosa] = FakeRepository(entities=[cosa])

        await repo.delete(cosa)

        assert len(repo) == 0
        assert cosa.id not in repo

    @pytest.mark.anyio
    async def test_registra_las_llamadas(self):
        """
        Para aseverar que un caso de uso no consultó dos veces lo mismo — el bug de rendimiento
        que un test contra una base no muestra.
        """
        cosa = Cosa()
        repo: FakeRepository[Cosa] = FakeRepository(entities=[cosa])

        await repo.get_by_id(cosa.id)
        await repo.get_by_id(cosa.id)
        await repo.list_all()

        assert repo.count_calls("get_by_id") == 2
        assert repo.count_calls("list_all") == 1
        assert repo.count_calls("save") == 0

    @pytest.mark.anyio
    async def test_registra_la_entidad_en_el_uow(self):
        """
        El repositorio real la registra para que sus eventos se despachen al commit. Omitirlo acá
        haría que un test de eventos de dominio pase con el fake y falle con el de verdad.
        """
        uow = FakeUnitOfWork()
        repo: FakeRepository[Cosa] = FakeRepository(uow)
        uow.add_repository("cosas", repo)

        await repo.save(Cosa())

        assert len(uow.collect_domain_events()) == 0  # `Cosa` no emite eventos
        assert repo.count_calls("save") == 1

    def test_seed_encadena(self):
        repo: FakeRepository[Cosa] = FakeRepository()

        resultado = repo.seed(Cosa(), Cosa())

        assert resultado is repo
        assert len(repo) == 2


# ── El Unit of Work genérico ──────────────────────────────────────────────────
class TestFakeUnitOfWork:
    @pytest.mark.anyio
    async def test_commit_cuenta_y_fija(self):
        uow = FakeUnitOfWork()
        repo: FakeRepository[Cosa] = FakeRepository(uow)
        uow.add_repository("cosas", repo)

        await repo.save(Cosa(nombre="una"))
        await uow.commit()

        assert uow.commits == 1
        assert len(repo) == 1

    @pytest.mark.anyio
    async def test_el_rollback_deshace_de_verdad(self):
        """
        ⚠️ Un doble que sólo cuenta la llamada hace que los tests de "la transacción se deshace"
        pasen sin probar nada.
        """
        uow = FakeUnitOfWork()
        repo: FakeRepository[Cosa] = FakeRepository(uow)
        uow.add_repository("cosas", repo)

        await repo.save(Cosa(nombre="commiteada"))
        await uow.commit()
        await repo.save(Cosa(nombre="perdida"))
        assert len(repo) == 2

        await uow.rollback()

        assert uow.rollbacks == 1
        assert [c.nombre for c in repo.stored] == ["commiteada"]

    @pytest.mark.anyio
    async def test_el_rollback_vuelve_al_ultimo_commit_y_no_al_inicio(self):
        uow = FakeUnitOfWork()
        repo: FakeRepository[Cosa] = FakeRepository(uow)
        uow.add_repository("cosas", repo)

        await repo.save(Cosa(nombre="uno"))
        await uow.commit()
        await repo.save(Cosa(nombre="dos"))
        await uow.commit()
        await repo.save(Cosa(nombre="tres"))

        await uow.rollback()

        assert [c.nombre for c in repo.stored] == ["uno", "dos"]

    @pytest.mark.anyio
    async def test_el_aexit_con_excepcion_rollbackea(self):
        """El contrato de `IUnitOfWork.__aexit__`, que el fake hereda tal cual."""
        uow = FakeUnitOfWork()
        repo: FakeRepository[Cosa] = FakeRepository(uow)
        uow.add_repository("cosas", repo)

        with pytest.raises(RuntimeError):
            async with uow:
                await repo.save(Cosa(nombre="perdida"))
                raise RuntimeError("algo falló")

        assert uow.rollbacks == 1
        assert len(repo) == 0

    @pytest.mark.anyio
    async def test_el_aexit_sin_excepcion_no_rollbackea(self):
        uow = FakeUnitOfWork()
        repo: FakeRepository[Cosa] = FakeRepository(uow)
        uow.add_repository("cosas", repo)

        async with uow:
            await repo.save(Cosa())
            await uow.commit()

        assert uow.rollbacks == 0
        assert len(repo) == 1

    @pytest.mark.anyio
    async def test_add_repository_re_toma_el_punto_de_guardado(self):
        """
        Un repositorio agregado después del `__init__` trae su propio estado inicial. Sin
        re-tomar el punto, un `rollback` lo borraría.
        """
        uow = FakeUnitOfWork()
        repo: FakeRepository[Cosa] = FakeRepository(entities=[Cosa(nombre="preexistente")])
        uow.add_repository("cosas", repo)

        await uow.rollback()

        assert [c.nombre for c in repo.stored] == ["preexistente"]

    @pytest.mark.anyio
    async def test_despacha_los_eventos_al_commit(self):
        publicados: list[object] = []

        class Bus:
            async def publish(self, evento: object) -> None:
                publicados.append(evento)

        uow = FakeUnitOfWork(event_bus=Bus())
        cosa = Cosa()
        object.__setattr__(cosa, "_events", ["un-evento"])
        uow.collect_entity(cosa)

        await uow.commit()

        assert publicados == ["un-evento"]
        assert uow.dispatched == ["un-evento"]

    @pytest.mark.anyio
    async def test_el_rollback_descarta_los_eventos(self):
        uow = FakeUnitOfWork()
        cosa = Cosa()
        object.__setattr__(cosa, "_events", ["un-evento"])
        uow.collect_entity(cosa)

        await uow.rollback()
        await uow.commit()

        assert uow.dispatched == []

    def test_add_repository_encadena(self):
        uow = FakeUnitOfWork()

        resultado = uow.add_repository("cosas", FakeRepository())

        assert resultado is uow


# ── El kit de Darwin ──────────────────────────────────────────────────────────
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from hexcore.darwin import reset_identity  # noqa: E402
from hexcore.darwin.testing import (  # noqa: E402
    AuditRecord,
    FakeRevocationList,
    FakeUserRepository,
    FakeVerificationRepository,
    PlainTextHasher,
    RecordingAuditSink,
    authenticated_context,
    configure_test_identity,
    create_test_user,
    impersonated_context,
    make_user,
    system_context,
)


class TestContextos:
    def test_authenticated_context(self):
        ana = make_user("ana@ejemplo.com")

        ctx = authenticated_context(ana, scopes=["facturas:leer"], roles=["staff"])

        assert ctx.actor_id == ana.id
        assert ctx.subject_id == ana.id
        assert ctx.is_impersonating is False
        assert ctx.has_scope("facturas:leer")
        assert ctx.has_role("staff")
        assert ctx.transport == "bearer"

    def test_acepta_un_id_suelto(self):
        """La mayoría de los tests sólo miran `actor_id`; construir un `User` para eso es ruido."""
        uid = uuid4()

        assert authenticated_context(uid).actor_id == uid

    def test_impersonated_context_arma_el_permiso(self):
        """
        ⚠️ El helper existe porque armarlo a mano falla: `AuthContext` se niega a existir si
        `subject != actor` sin `Impersonation`.
        """
        soporte = make_user("soporte@ejemplo.com")
        cliente = make_user("cliente@ejemplo.com")

        ctx = impersonated_context(soporte, cliente, reason="ticket #4821")

        assert ctx.is_impersonating is True
        assert ctx.actor_id == soporte.id
        assert ctx.subject_id == cliente.id
        assert ctx.impersonation is not None
        assert ctx.impersonation.reason == "ticket #4821"
        assert ctx.impersonation.granted_by == soporte.id

    def test_armarlo_a_mano_falla_y_por_eso_esta_el_helper(self):
        """El test que documenta el motivo del helper."""
        from hexcore.darwin import AuthContext, Principal

        soporte = Principal(user_id=uuid4(), session_id=uuid4())
        cliente = Principal(user_id=uuid4(), session_id=uuid4())

        with pytest.raises(Exception, match="impersonación"):
            AuthContext(actor=soporte, subject=cliente, transport="bearer")

    def test_el_techo_es_de_una_hora(self):
        from hexcore.darwin.application.services import IMPERSONATION_CAP

        ctx = impersonated_context(
            make_user("a@x.com"), make_user("b@x.com"), granted_at=AHORA
        )

        assert ctx.impersonation is not None
        assert ctx.impersonation.expires_at == AHORA + IMPERSONATION_CAP

    def test_impersonarse_a_uno_mismo_falla_con_un_mensaje_util(self):
        ana = make_user("ana@ejemplo.com")

        with pytest.raises(ValueError, match="authenticated_context"):
            impersonated_context(ana, ana)

    def test_los_scopes_son_del_actor(self):
        """
        Impersonar no presta permisos. Si el helper los tomara del subject, un test estaría
        probando algo que Darwin no hace.
        """
        soporte = make_user("soporte@ejemplo.com", scopes=["soporte"])
        cliente = make_user("cliente@ejemplo.com", scopes=["secreto"])

        ctx = impersonated_context(soporte, cliente, scopes=["soporte"])

        assert ctx.has_scope("soporte")
        assert not ctx.has_scope("secreto")

    def test_system_context(self):
        ctx = system_context("cron:cerrar", scopes=["register.close"])

        assert ctx.is_system is True
        assert ctx.transport == "worker"
        assert ctx.has_scope("register.close")
        assert not ctx.has_scope("usuarios:borrar"), "no es un superusuario"

    def test_make_user_pone_los_scopes_en_extra(self):
        """Es donde los lee la política de `impersonate`."""
        ana = make_user("ana@ejemplo.com", scopes=["admin"], plan="pro")

        assert ana.extra == {"plan": "pro", "scopes": ["admin"]}


class TestFakesDeDarwin:
    @pytest.mark.anyio
    async def test_el_usuario_se_indexa_por_mail(self):
        ana = make_user("ana@ejemplo.com")
        repo = FakeUserRepository([ana])

        assert (await repo.get_by_email("ana@ejemplo.com")).id == ana.id
        assert await repo.get_by_email("nadie@ejemplo.com") is None

    @pytest.mark.anyio
    async def test_un_mail_repetido_lanza(self):
        """
        Igual que el `UNIQUE` real. Dejarlo pasar haría que un test de "no se puede registrar dos
        veces" pase sin probar nada.
        """
        from hexcore.darwin import EmailAlreadyRegisteredError

        repo = FakeUserRepository([make_user("ana@ejemplo.com")])

        with pytest.raises(EmailAlreadyRegisteredError):
            await repo.add(make_user("ana@ejemplo.com"))

    @pytest.mark.anyio
    async def test_cambiar_el_mail_saca_la_entrada_vieja(self):
        """
        Sin esto, el mail anterior seguiría resolviendo al usuario y un test de "cambié mi mail"
        pasaría con las dos direcciones funcionando.
        """
        ana = make_user("ana@ejemplo.com")
        repo = FakeUserRepository([ana])

        await repo.update(ana.model_copy(update={"email": "nueva@ejemplo.com"}))

        assert await repo.get_by_email("ana@ejemplo.com") is None
        assert (await repo.get_by_email("nueva@ejemplo.com")).id == ana.id

    @pytest.mark.anyio
    async def test_bump_token_generation_es_atomico(self):
        """
        Es la capa 3 de la revocación: con un `await` en el medio, dos revocaciones masivas
        concurrentes subirían una sola generación y la mitad de los tokens seguiría valiendo.
        """
        ana = make_user("ana@ejemplo.com")
        repo = FakeUserRepository([ana])

        resultados = await asyncio.gather(
            *(repo.bump_token_generation(ana.id) for _ in range(10))
        )

        assert sorted(resultados) == list(range(1, 11))
        assert (await repo.get_by_id(ana.id)).token_generation == 10

    @pytest.mark.anyio
    async def test_el_canje_de_verificacion_es_atomico(self):
        """De dos canjes concurrentes gana uno, igual que el `UPDATE ... RETURNING` real."""
        from hexcore.darwin import Verification

        v = Verification(
            identifier="ana@ejemplo.com",
            value_hash="h",
            purpose="email_verification",
            expires_at=AHORA + timedelta(hours=1),
        )
        repo = FakeVerificationRepository([v])

        resultados = await asyncio.gather(
            *(
                repo.consume("ana@ejemplo.com", "email_verification", "h", at=AHORA)
                for _ in range(8)
            )
        )

        assert sum(1 for r in resultados if r is not None) == 1

    @pytest.mark.anyio
    async def test_el_canje_filtra_por_purpose(self):
        """Un código de reset no se canjea en el flujo de verificar el mail."""
        from hexcore.darwin import Verification

        repo = FakeVerificationRepository(
            [
                Verification(
                    identifier="ana@ejemplo.com",
                    value_hash="h",
                    purpose="password_reset",
                    expires_at=AHORA + timedelta(hours=1),
                )
            ]
        )

        assert (
            await repo.consume(
                "ana@ejemplo.com", "email_verification", "h", at=AHORA
            )
            is None
        )

    @pytest.mark.anyio
    async def test_invalidate_for_marca_los_pendientes(self):
        from hexcore.darwin import Verification

        repo = FakeVerificationRepository(
            [
                Verification(
                    identifier="ana@ejemplo.com",
                    value_hash=f"h{i}",
                    purpose="magic_link",
                    expires_at=AHORA + timedelta(hours=1),
                )
                for i in range(3)
            ]
        )

        assert await repo.invalidate_for("ana@ejemplo.com", "magic_link", at=AHORA) == 3
        assert (
            await repo.consume("ana@ejemplo.com", "magic_link", "h0", at=AHORA) is None
        )

    @pytest.mark.anyio
    async def test_la_denylist_vence(self):
        """
        La real guarda el vencimiento **dentro del valor** porque `MemoryCache.set()` ignora
        `expire` y nunca desaloja. Un fake que no venciera esconderia ese bug.
        """
        from hexcore.darwin import FixedClock

        reloj = FixedClock(AHORA)
        lista = FakeRevocationList(clock=reloj)
        sid = uuid4()

        await lista.revoke(sid, until=AHORA + timedelta(minutes=2))
        assert await lista.is_revoked(sid) is True

        reloj.advance(minutes=3)
        assert await lista.is_revoked(sid) is False

    @pytest.mark.anyio
    async def test_el_hasher_de_test_verifica_lo_que_hashea(self):
        hasher = PlainTextHasher()

        hashed = hasher.hash("una frase")

        assert hasher.verify("una frase", hashed) is True
        assert hasher.verify("otra", hashed) is False
        assert hashed.startswith("plain$"), "el prefijo hace visible el doble en un dump"

    def test_el_hasher_cuenta_los_dummy(self):
        """
        Es lo que permite aseverar que el camino de "mail inexistente" iguala el tiempo — el
        chequeo que evita la enumeración de usuarios.
        """
        hasher = PlainTextHasher()

        hasher.hash_dummy()
        hasher.hash_dummy()

        assert hasher.dummy_calls == 2

    @pytest.mark.anyio
    async def test_el_sink_de_auditoria_registra(self):
        sink = RecordingAuditSink()
        actor, subject = uuid4(), uuid4()

        await sink.record(
            action="impersonation.start",
            actor_id=actor,
            subject_id=subject,
            impersonated=True,
            metadata={"reason": "ticket"},
        )

        assert sink.actions == ["impersonation.start"]
        assert isinstance(sink.last, AuditRecord)
        assert sink.last.impersonated is True
        assert sink.last.metadata == {"reason": "ticket"}
        assert len(sink.for_action("impersonation.start")) == 1
        assert sink.for_action("otra") == []

    def test_last_lanza_con_la_lista_vacia(self):
        """
        Un test que asevera sobre `last` sin registros tiene que fallar ahí, no en el atributo
        siguiente.
        """
        with pytest.raises(IndexError):
            RecordingAuditSink().last


class TestCableadoSinBase:
    """
    El cableado completo con los fakes. **Ningún test de esta clase toca disco**, y eso es todo el
    punto del kit: un consumidor prueba su caso de uso sin levantar un motor.
    """

    @pytest.fixture
    def contenedor(self):
        contenedor = configure_test_identity()
        yield contenedor
        reset_identity()

    @pytest.mark.anyio
    async def test_el_sign_in_completo_sin_base(self, contenedor):
        await create_test_user(contenedor, "ana@ejemplo.com")

        usuario, sesion, par = await contenedor.identity_service().sign_in(
            email="ana@ejemplo.com", password="una frase larga y buena"
        )

        assert usuario.email == "ana@ejemplo.com"
        assert sesion.actor_user_id == usuario.id
        assert par.access_token and par.refresh_token

    @pytest.mark.anyio
    async def test_el_token_que_sale_es_de_verdad(self, contenedor):
        """
        Se firma con una clave Ed25519 real y verifica de verdad. Falsear la firma haría que un
        test de confusión de `alg` no pruebe nada.
        """
        await create_test_user(contenedor, "ana@ejemplo.com")
        _, _, par = await contenedor.identity_service().sign_in(
            email="ana@ejemplo.com", password="una frase larga y buena"
        )

        ctx = await contenedor.session_service().authenticate(
            par.access_token, transport="cookie"
        )

        assert ctx.actor_id == ctx.subject_id
        assert ctx.is_impersonating is False

    @pytest.mark.anyio
    async def test_la_rotacion_de_refresh_funciona(self, contenedor):
        await create_test_user(contenedor, "ana@ejemplo.com")
        _, _, par = await contenedor.identity_service().sign_in(
            email="ana@ejemplo.com", password="una frase larga y buena",
            transport="bearer",
        )

        _, nuevo = await contenedor.session_service().refresh(
            par.refresh_token, transport="bearer"
        )

        assert nuevo.access_token != par.access_token

    @pytest.mark.anyio
    async def test_la_deteccion_de_reuso_funciona_con_los_fakes(self, contenedor):
        """
        La prueba de que la atomicidad del fake alcanza: sin `consume_for_rotation` de un solo
        paso, la detección de reuso no dispararía nunca.
        """
        from hexcore.darwin import TokenRevokedError

        await create_test_user(contenedor, "ana@ejemplo.com")
        _, _, par = await contenedor.identity_service().sign_in(
            email="ana@ejemplo.com", password="una frase larga y buena",
            transport="bearer",
        )
        await contenedor.session_service().refresh(
            par.refresh_token, transport="bearer"
        )

        # El reloj está fijo, así que hay que salir de la ventana de gracia a mano.
        contenedor.clock().advance(seconds=30)

        with pytest.raises(TokenRevokedError):
            await contenedor.session_service().refresh(
                par.refresh_token, transport="bearer"
            )

    @pytest.mark.anyio
    async def test_un_usuario_sembrado_no_tiene_contrasena(self, contenedor):
        """
        Es el estado correcto de quien entra sólo por OAuth o por passkey, y el error más común al
        usar el kit — por eso `create_test_user` existe y lo dice el docstring.
        """
        from hexcore.darwin import InvalidCredentialsError, reset_identity as limpiar

        limpiar()
        cont = configure_test_identity(seed_users=[make_user("ana@ejemplo.com")])
        try:
            with pytest.raises(InvalidCredentialsError):
                await cont.identity_service().sign_in(
                    email="ana@ejemplo.com", password="cualquiera"
                )
        finally:
            limpiar()

    @pytest.mark.anyio
    async def test_la_auditoria_se_puede_aseverar(self):
        """
        Con el sink cableado, el reuso de un refresh queda registrado con los dos principales.

        Es el único punto que `SessionService` audita, y es el que importa: la detección de reuso
        es una de las pocas señales inequívocas de compromiso que un sistema de auth puede emitir.
        """
        from hexcore.darwin import TokenRevokedError

        sink = RecordingAuditSink()
        contenedor = configure_test_identity(audit=sink)
        try:
            usuario = await create_test_user(contenedor, "ana@ejemplo.com")
            _, _, par = await contenedor.identity_service().sign_in(
                email="ana@ejemplo.com",
                password="una frase larga y buena",
                transport="bearer",
            )
            await contenedor.session_service().refresh(
                par.refresh_token, transport="bearer"
            )
            contenedor.clock().advance(seconds=30)  # fuera de la ventana de gracia

            with pytest.raises(TokenRevokedError):
                await contenedor.session_service().refresh(
                    par.refresh_token, transport="bearer"
                )

            assert sink.actions == ["session.reuse_detected"]
            assert sink.last.actor_id == usuario.id
            assert sink.last.metadata["revoked"] >= 1
        finally:
            reset_identity()

    @pytest.mark.anyio
    async def test_un_puerto_se_puede_reemplazar(self, contenedor):
        """
        El `**overrides` de `configure_test_identity`, para el test que necesita un puerto propio.

        `users=` es la clave del **puerto** —igual que en `configure_identity`— y `seed_users=` es
        la lista de usuarios. Con un solo nombre para las dos cosas, pasar un repositorio sembraba
        un repositorio como si fuera una lista, y el error salía tres capas abajo.
        """
        from hexcore.darwin import reset_identity as limpiar

        limpiar()
        propio = FakeUserRepository([make_user("propia@ejemplo.com")])
        cont = configure_test_identity(users=propio)
        try:
            assert cont.users() is propio
            assert await cont.users().get_by_email("propia@ejemplo.com") is not None
        finally:
            limpiar()

    @pytest.mark.anyio
    async def test_seed_users_siembra(self, contenedor):
        from hexcore.darwin import reset_identity as limpiar

        limpiar()
        cont = configure_test_identity(seed_users=[make_user("sembrada@ejemplo.com")])
        try:
            assert await cont.users().get_by_email("sembrada@ejemplo.com") is not None
        finally:
            limpiar()

    @pytest.mark.anyio
    async def test_el_reloj_es_controlable(self, contenedor):
        from hexcore.darwin import TokenExpiredError

        await create_test_user(contenedor, "ana@ejemplo.com")
        _, _, par = await contenedor.identity_service().sign_in(
            email="ana@ejemplo.com", password="una frase larga y buena",
            transport="bearer",
        )

        contenedor.clock().advance(days=400)

        with pytest.raises(TokenExpiredError):
            await contenedor.session_service().authenticate(
                par.access_token, transport="bearer"
            )

    @pytest.mark.anyio
    async def test_un_plugin_se_puede_cablear(self):
        """El kit sirve para probar plugins, que es la mitad de lo que un consumidor extiende."""
        from hexcore.darwin import PluginRegistry
        from hexcore.darwin.plugins.magic_link import MagicLinkPlugin

        contenedor = configure_test_identity(
            plugins=PluginRegistry([MagicLinkPlugin()])
        )
        try:
            assert contenedor.plugins.names == ("magic_link",)
        finally:
            reset_identity()


class TestFixtures:
    """
    Las fixtures del plugin de pytest. Se prueban vía `pytest_plugins` en el `conftest.py` de la
    suite; acá se verifica que existan y que hagan lo que dicen.
    """

    def test_el_modulo_declara_sus_fixtures(self):
        from hexcore.darwin.testing import fixtures

        for nombre in (
            "identity_clock",
            "identity_container",
            "identity_audit",
            "identity_users",
        ):
            assert nombre in fixtures.__all__
            assert hasattr(fixtures, nombre)

    def test_el_instante_de_test_es_fijo(self):
        """
        Un reloj que arranca en `now()` hace que un test de vencimiento falle una vez al año,
        cuando el cambio de horario mueve el offset.
        """
        from hexcore.darwin.testing.fixtures import AHORA_DE_TEST

        assert AHORA_DE_TEST.tzinfo is not None
        assert AHORA_DE_TEST == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# ── Nada del kit exige los extras ─────────────────────────────────────────────
def test_el_kit_no_arrastra_sqlalchemy():
    """
    `import hexcore.darwin.testing` tiene que funcionar en un proceso sin `[sql]`: los fakes
    existen justamente para no necesitarlo.

    Se corre en un subproceso porque en este intérprete sqlalchemy ya está importado por el resto
    de la suite.
    """
    import subprocess
    import sys

    codigo = (
        "import sys\n"
        "class F:\n"
        "    @classmethod\n"
        "    def find_spec(cls, fullname, path, target=None):\n"
        "        if fullname.split('.')[0] == 'sqlalchemy':\n"
        "            raise ImportError('bloqueado')\n"
        "        return None\n"
        "sys.meta_path.insert(0, F)\n"
        "import hexcore.darwin.testing as m\n"
        "assert m.make_user('a@b.c').email == 'a@b.c'\n"
        "print('ok')\n"
    )

    resultado = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True
    )

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert "ok" in resultado.stdout
