"""
Roles, scopes y la tercera capa de revocación.

Tres defectos que se arreglan juntos porque son el mismo agujero visto desde tres lados: los
permisos de una sesión no llegaban a ningún lado.

1. **Por HTTP los scopes salían siempre vacíos.** `POST /auth/sign-in` llama a
   `IdentityService.sign_in()` sin pasar `scopes`, y el parámetro tenía default `()`. No había
   forma de poblarlos sin llamar al servicio a mano, así que `auth.require_scopes(...)`
   rechazaba a todo el mundo en cualquier app que usara el router.
2. **La rotación los perdía.** `_rotar` armaba el `Principal` de la sesión siguiente sin
   `scopes=`, y la entidad `IdentitySession` ni siquiera tenía dónde guardarlos. El usuario
   entraba bien y a los dos minutos —el `access_ttl`— perdía el acceso.
3. **`GenerationGuard` no lo usaba nadie.** La clase estaba escrita, documentada con ejemplo y
   exportada en el facade, pero ningún camino la construía: `SignOutEverywhere` incrementaba el
   contador y `authenticate` no lo miraba, así que "cerrar sesión en todos los dispositivos" no
   cerraba ninguna hasta que cada token venciera solo.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from hexcore.darwin import (  # noqa: E402
    AbstractPrincipalResolver,
    AccountLockedError,
    IdentityConfig,
    NullPrincipalResolver,
    ResolvedPrincipal,
    TokenRevokedError,
)
from hexcore.darwin.testing import (  # noqa: E402
    TEST_SECRET_KEY,
    configure_test_identity,
    create_test_user,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ResolverFijo(AbstractPrincipalResolver):
    """Devuelve siempre lo mismo, y cuenta cuántas veces se lo preguntaron."""

    def __init__(
        self,
        roles: frozenset[str],
        scopes: frozenset[str],
        status: str | None = None,
    ) -> None:
        self.roles = roles
        self.scopes = scopes
        self.status = status
        self.llamadas = 0

    async def resolve(self, user):  # noqa: ANN001, ANN201
        del user
        self.llamadas += 1
        return ResolvedPrincipal(
            roles=self.roles, scopes=self.scopes, status=self.status
        )


def _config() -> IdentityConfig:
    return IdentityConfig(
        secret_key=TEST_SECRET_KEY, require_verified_email=False
    )


class TestElResolverPueblaLaSesion:
    @pytest.mark.anyio
    async def test_el_sign_in_sin_scopes_explicitos_le_pregunta_al_resolver(self) -> None:
        """
        El defecto 1. La ruta HTTP no pasa `scopes`, así que si "no pasar" significara "ninguno"
        no habría forma de poblarlos.
        """
        resolver = ResolverFijo(frozenset({"admin"}), frozenset({"facturas.leer"}))
        contenedor = configure_test_identity(_config(), principals=resolver)
        servicio = contenedor.identity_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, sesion, _ = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )

        assert resolver.llamadas == 1, "El sign-in no consultó al resolver."
        assert sesion.scopes == frozenset({"facturas.leer"}), (
            f"La sesión se guardó con scopes {sesion.scopes!r}. Si están vacíos, el sign-in "
            f"volvió a afirmar 'ninguno' en vez de preguntar."
        )

    @pytest.mark.anyio
    async def test_los_roles_y_scopes_vuelven_en_el_auth_context(self) -> None:
        """
        Viajan en el token, no en la base: `authenticate` es el camino caliente y no consulta.
        """
        resolver = ResolverFijo(frozenset({"admin"}), frozenset({"facturas.leer"}))
        contenedor = configure_test_identity(_config(), principals=resolver)
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )

        contexto = await sesiones.authenticate(par.access_token, transport="cookie")
        assert contexto.actor.roles == frozenset({"admin"}), (
            f"Los roles no sobrevivieron al token: {contexto.actor.roles!r}."
        )
        assert contexto.actor.scopes == frozenset({"facturas.leer"})
        assert contexto.actor.has_role("admin")
        assert contexto.actor.has_scope("facturas.leer")

    @pytest.mark.anyio
    async def test_unos_scopes_explicitos_ganan_sobre_el_resolver(self) -> None:
        """
        Pasar un iterable —aunque sea vacío— es declarar los permisos a mano. Es lo que hace el
        plugin de impersonación, que no quiere los del actor.
        """
        resolver = ResolverFijo(frozenset({"admin"}), frozenset({"todo"}))
        contenedor = configure_test_identity(_config(), principals=resolver)
        servicio = contenedor.identity_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, sesion, _ = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga", scopes=["solo.esto"]
        )

        assert sesion.scopes == frozenset({"solo.esto"})

    @pytest.mark.anyio
    async def test_el_default_no_da_permisos(self) -> None:
        """El comportamiento de 9.x: una app que no declara permisos no empieza a recibirlos."""
        contenedor = configure_test_identity(_config())
        assert isinstance(contenedor.principals(), NullPrincipalResolver)

        servicio = contenedor.identity_service()
        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, sesion, _ = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )
        assert sesion.scopes == frozenset()


class TestElEstadoDeLaCuenta:
    """
    El estado **de la app**, no el de Darwin.

    Darwin sólo sabe de `is_active` y `locked_until`, y sólo los mira al rotar. Una app con su
    propia máquina de estados —`pending`, `suspended`, `banned`— tenía dos opciones malas:
    consultar la base en cada request para saber si el usuario sigue habilitado, o espejar su
    estado dentro de `is_active`/`locked_until` en cada transición y mantener los dos
    sincronizados para siempre.

    Ahora el estado lo aporta el mismo resolver que ya da roles y scopes, viaja en el token, y
    se lee del `AuthContext` sin consultar nada.
    """

    @pytest.mark.anyio
    async def test_el_estado_llega_al_auth_context_sin_consultar(self) -> None:
        resolver = ResolverFijo(frozenset(), frozenset(), status="suspended")
        contenedor = configure_test_identity(_config(), principals=resolver)
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )

        llamadas_antes = resolver.llamadas
        contexto = await sesiones.authenticate(par.access_token, transport="cookie")

        assert contexto.actor.status == "suspended"
        assert resolver.llamadas == llamadas_antes, (
            "`authenticate` consultó al resolver. El estado tiene que venir del token: el "
            "punto del campo es que el camino caliente no consulte nada."
        )

    @pytest.mark.anyio
    async def test_el_estado_se_re_resuelve_al_rotar(self) -> None:
        """
        La ventana es de un `access_ttl`. Es el mismo compromiso que ya rige para roles y
        scopes, y es lo que hace que la app no tenga que espejar nada en Darwin.
        """
        resolver = ResolverFijo(frozenset(), frozenset(), status="active")
        contenedor = configure_test_identity(_config(), principals=resolver)
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )
        assert par.refresh_token is not None

        resolver.status = "suspended"
        _, par_nuevo = await sesiones.refresh(par.refresh_token, transport="cookie")

        contexto = await sesiones.authenticate(par_nuevo.access_token, transport="cookie")
        assert contexto.actor.status == "suspended"

    @pytest.mark.anyio
    async def test_sin_resolver_el_estado_es_none(self) -> None:
        """Una app que no lo usa no empieza a recibir un estado inventado."""
        contenedor = configure_test_identity(_config())
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )

        contexto = await sesiones.authenticate(par.access_token, transport="cookie")
        assert contexto.actor.status is None

    @pytest.mark.anyio
    async def test_el_resolver_puede_abortar_el_sign_in(self) -> None:
        """
        El corte duro para un estado que no puede entrar. Tiene que ser una `IdentityError`:
        cualquier otra excepción escapa al mapeo del módulo y sale como 500.
        """

        class ResolverQueRechaza(AbstractPrincipalResolver):
            async def resolve(self, user):  # noqa: ANN001, ANN201
                del user
                raise AccountLockedError("La cuenta está suspendida.")

        contenedor = configure_test_identity(
            _config(), principals=ResolverQueRechaza()
        )
        servicio = contenedor.identity_service()
        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )

        with pytest.raises(AccountLockedError):
            await servicio.sign_in(
                identifier="ana@ejemplo.com", password="una-frase-larga"
            )

    @pytest.mark.anyio
    async def test_el_estado_no_lo_puede_declarar_el_llamador(self) -> None:
        """
        Aunque se declaren roles y scopes explícitos, el resolver **igual** se consulta: el
        estado no es un permiso y no se pasa por parámetro. Si se salteara la consulta al
        declararlos, el estado quedaría siempre en `None` para el plugin de impersonación y
        cualquier otro que los declare.
        """
        resolver = ResolverFijo(frozenset({"admin"}), frozenset({"todo"}), status="active")
        contenedor = configure_test_identity(_config(), principals=resolver)
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com",
            password="una-frase-larga",
            scopes=["solo.esto"],
        )

        contexto = await sesiones.authenticate(par.access_token, transport="cookie")
        assert contexto.actor.scopes == frozenset({"solo.esto"})
        assert contexto.actor.status == "active"


class TestLaRotacionNoPierdeLosPermisos:
    @pytest.mark.anyio
    async def test_los_scopes_sobreviven_un_refresh(self) -> None:
        """
        El defecto 2, y la regresión que más se nota: entrar bien y perder el acceso a los dos
        minutos.
        """
        resolver = ResolverFijo(frozenset({"admin"}), frozenset({"facturas.leer"}))
        contenedor = configure_test_identity(_config(), principals=resolver)
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )
        assert par.refresh_token is not None

        rotada, par_nuevo = await sesiones.refresh(par.refresh_token, transport="cookie")

        assert rotada.scopes == frozenset({"facturas.leer"}), (
            f"La sesión rotada quedó con scopes {rotada.scopes!r}."
        )
        contexto = await sesiones.authenticate(par_nuevo.access_token, transport="cookie")
        assert contexto.actor.scopes == frozenset({"facturas.leer"}), (
            "El access token emitido al rotar salió sin scopes: es exactamente el bug, "
            "el usuario pierde el acceso al primer refresh."
        )
        assert contexto.actor.roles == frozenset({"admin"})

    @pytest.mark.anyio
    async def test_sin_resolver_los_scopes_se_restauran_de_la_fila(self) -> None:
        """
        El respaldo. Con el `NullPrincipalResolver` por defecto, re-resolver daría vacío — y sin
        fallback un llamador que creó la sesión con scopes explícitos los perdería igual.
        """
        contenedor = configure_test_identity(_config())
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga", scopes=["solo.esto"]
        )
        assert par.refresh_token is not None

        rotada, _ = await sesiones.refresh(par.refresh_token, transport="cookie")
        assert rotada.scopes == frozenset({"solo.esto"})

    @pytest.mark.anyio
    async def test_re_resolver_aplica_un_permiso_quitado(self) -> None:
        """
        La contracara de re-resolver en cada rotación, y la razón de hacerlo: quitarle un rol a
        alguien tiene efecto sin esperar a que cierre sesión.
        """
        resolver = ResolverFijo(frozenset({"admin"}), frozenset({"facturas.borrar"}))
        contenedor = configure_test_identity(_config(), principals=resolver)
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )
        assert par.refresh_token is not None

        # Le sacan el permiso mientras la sesión está viva.
        resolver.roles = frozenset()
        resolver.scopes = frozenset({"facturas.leer"})

        rotada, _ = await sesiones.refresh(par.refresh_token, transport="cookie")
        assert rotada.scopes == frozenset({"facturas.leer"}), (
            "La rotación copió los scopes viejos de la fila en vez de volver a resolverlos, "
            "así que quitar un permiso no tiene efecto hasta el próximo login."
        )


class TestLaCuentaSinDerechoNoRenueva:
    @pytest.mark.anyio
    async def test_una_cuenta_desactivada_no_puede_rotar(self) -> None:
        """
        Sin esto, desactivar una cuenta no echaba a quien ya estaba adentro: su sesión seguía
        rotando indefinidamente.
        """
        contenedor = configure_test_identity(_config())
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()
        usuarios = contenedor.users()

        usuario = await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )
        assert par.refresh_token is not None

        await usuarios.update(usuario.model_copy(update={"is_active": False}))

        with pytest.raises(TokenRevokedError, match="desactivada"):
            await sesiones.refresh(par.refresh_token, transport="cookie")

    @pytest.mark.anyio
    async def test_una_cuenta_bloqueada_no_puede_rotar(self) -> None:
        contenedor = configure_test_identity(_config())
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()
        usuarios = contenedor.users()
        reloj = contenedor.clock()

        usuario = await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )
        assert par.refresh_token is not None

        await usuarios.update(
            usuario.model_copy(
                update={"locked_until": reloj.now() + timedelta(hours=1)}
            )
        )

        with pytest.raises(TokenRevokedError, match="bloqueada"):
            await sesiones.refresh(par.refresh_token, transport="cookie")


class TestLaTerceraCapaDeRevocacion:
    @pytest.mark.anyio
    async def test_cerrar_todas_las_sesiones_corta_el_token_vigente(self) -> None:
        """
        El defecto 3, de punta a punta: "cerrá sesión en todos los dispositivos" tiene que
        cortar el access token **vigente**, no esperar a que venza ni a la rotación siguiente.

        Se ejercita `revoke_all_for` y no `bump_token_generation` suelto porque el corte son
        **dos** operaciones: subir el contador y descartar la generación cacheada. Con la
        primera sola, el cache de 60 s de `GenerationGuard` sigue devolviendo la generación
        vieja y el token sigue pasando durante todo ese minuto.
        """
        contenedor = configure_test_identity(_config())
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()

        usuario = await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )

        # Antes del corte, el token sirve. Esto además **puebla el cache** de generación, que
        # es lo que hace que el test valga: sin invalidarlo, el corte no se vería.
        await sesiones.authenticate(par.access_token, transport="cookie")

        await sesiones.revoke_all_for(usuario.id, reason="test")

        with pytest.raises(TokenRevokedError):
            await sesiones.authenticate(par.access_token, transport="cookie")

    @pytest.mark.anyio
    async def test_authenticate_compara_la_generacion_del_token(self) -> None:
        """
        La capa 3 aislada: un token con `gen` viejo se rechaza aunque su `sid` no esté en la
        denylist. Es lo que permite revocar N sesiones con un solo UPDATE.
        """
        contenedor = configure_test_identity(_config())
        servicio = contenedor.identity_service()
        sesiones = contenedor.session_service()
        usuarios = contenedor.users()

        usuario = await create_test_user(
            contenedor, email="ana@ejemplo.com", password="una-frase-larga"
        )
        _, _, par = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )

        await usuarios.bump_token_generation(usuario.id)
        await contenedor.generations().invalidate_cache(usuario.id)

        with pytest.raises(TokenRevokedError):
            await sesiones.authenticate(par.access_token, transport="cookie")

    @pytest.mark.anyio
    async def test_el_contenedor_cablea_el_guard(self) -> None:
        """Estaba escrito y exportado, y no lo construía nadie. Ese era todo el defecto."""
        from hexcore.darwin.infrastructure.revocation import GenerationGuard

        contenedor = configure_test_identity(_config())
        assert isinstance(contenedor.generations(), GenerationGuard)
