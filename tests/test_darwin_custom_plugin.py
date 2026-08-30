"""
El plugin de ejemplo de `docs/darwin/plugins-propios.md`, ejecutado.

Una guía de extensión que no corre es una guía que envejece sin avisar: el día que un punto
de extensión cambia de firma, el documento sigue diciendo lo de antes y el primero en
enterarse es alguien que ya escribió medio plugin siguiéndolo.

Así que el plugin de la guía vive acá, entero, y estos tests son los que le dan valor de
verdad al documento: si la guía miente, esto se pone rojo.

**Si editás la guía, editá esto — y al revés.** El código de los dos lados tiene que ser el
mismo; lo único que cambia es que allá está explicado y acá está ejercitado.
"""
from __future__ import annotations

import typing as t

import pytest

from hexcore.darwin.application.plugins import PluginError, PluginRegistry
from hexcore.darwin.domain.exceptions import IdentityError
from hexcore.darwin.domain.plugins import (
    DarwinPlugin,
    HookBinding,
    ShortCircuit,
    action_of,
    identity_action,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── El plugin de la guía ──────────────────────────────────────────────────────
#
# Un plugin de "horario permitido": bloquea el sign-in fuera de una ventana horaria. Se eligió
# porque toca las tres cosas que un plugin real toca —un hook `before`, un cortocircuito y
# configuración por constructor— sin necesitar tabla propia, que es el caso que complica el
# ejemplo sin enseñar nada nuevo.


class FueraDeHorarioError(IdentityError):
    """
    El plugin corta el sign-in fuera de la ventana permitida.

    Hereda de `IdentityError` y **no** de `Exception`, y esa es la diferencia entre un plugin
    que funciona y uno que da 500. `run_hooks` deja propagar tal cual sólo `ShortCircuit` y
    `IdentityError`; cualquier otra excepción la envuelve en un `RuntimeError` que nombra al
    plugin, porque un hook que explota no puede leerse como un hook que aprobó.

    O sea: una señal deliberada del dominio se declara como tal, o el framework la trata como
    un plugin roto — que es lo correcto para un `ValueError` inesperado y lo incorrecto para
    un rechazo que el plugin quiso emitir.
    """


class HorarioPermitidoPlugin(DarwinPlugin):
    """Rechaza el sign-in fuera de una ventana horaria."""

    name = "horario_permitido"
    priority = 10

    def __init__(self, *, desde: int = 8, hasta: int = 20) -> None:
        self._desde = desde
        self._hasta = hasta
        self.vistos: list[str] = []

    def hooks(self) -> t.Sequence[HookBinding]:
        return [
            HookBinding(
                action="user.sign_in",
                phase="before",
                handler=self._verificar_horario,
                priority=10,
            ),
            HookBinding(
                action="*",
                phase="after",
                handler=self._registrar,
                priority=900,
            ),
        ]

    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        return {FueraDeHorarioError: 403}

    async def _verificar_horario(self, payload: t.Any) -> t.Any:
        hora = getattr(payload, "hora", None)
        if hora is not None and not (self._desde <= hora < self._hasta):
            raise FueraDeHorarioError(
                f"El acceso está permitido de {self._desde} a {self._hasta}."
            )
        return None

    async def _registrar(self, payload: t.Any) -> t.Any:
        self.vistos.append(type(payload).__name__)
        return None


# ── Un plugin que cortocircuita, para el otro camino ──────────────────────────
class CachePlugin(DarwinPlugin):
    """Responde por su cuenta y saltea el handler."""

    name = "cache"

    def __init__(self, respuesta: t.Any) -> None:
        self._respuesta = respuesta

    def hooks(self) -> t.Sequence[HookBinding]:
        return [
            HookBinding(
                action="user.sign_in",
                phase="before",
                handler=self._responder,
                priority=1,
            )
        ]

    async def _responder(self, payload: t.Any) -> t.Any:
        raise ShortCircuit(self._respuesta)


# ── Lo mínimo que exige el registro ───────────────────────────────────────────
class SinNombre(DarwinPlugin):
    """No declara `name`. El registro tiene que rechazarlo."""


class DependeDeAlguien(DarwinPlugin):
    name = "depende"
    requires = ("no_existe",)


# ── Tests ─────────────────────────────────────────────────────────────────────
class TestLoMinimo:
    def test_un_plugin_solo_necesita_name(self):
        """
        Todos los puntos de extensión son concretos y devuelven vacío.

        Es lo que hace que agregar un punto de extensión nuevo en una versión futura no rompa
        a ningún plugin existente: con métodos abstractos, cada plugin tendría que implementar
        ocho cosas para aportar una.
        """

        class Minimo(DarwinPlugin):
            name = "minimo"

        registro = PluginRegistry([Minimo()])
        registro.validate()

        assert registro.names == ("minimo",)
        assert Minimo().hooks() == ()
        assert Minimo().routers() == ()
        assert Minimo().tables() == {}
        assert Minimo().exception_status_map() == {}

    def test_sin_name_el_registro_lo_rechaza(self):
        with pytest.raises(PluginError) as excinfo:
            PluginRegistry([SinNombre()])

        assert "name" in str(excinfo.value)

    def test_un_requires_inexistente_se_rechaza_al_validar(self):
        """
        Al cablear, no en el primer request. Un plugin que declara depender de otro que nadie
        registró correría igual, sin lo que necesita, y fallaría más adelante en un lugar que
        no señala la causa.
        """
        registro = PluginRegistry([DependeDeAlguien()])

        with pytest.raises(PluginError) as excinfo:
            registro.validate()

        assert "no_existe" in str(excinfo.value)


class TestHooks:
    @pytest.mark.anyio
    async def test_el_hook_deja_pasar_dentro_del_horario(self):
        from hexcore.darwin.application.hooks import run_hooks

        plugin = HorarioPermitidoPlugin(desde=8, hasta=20)
        registro = PluginRegistry([plugin])
        registro.validate()

        class SignIn:
            hora = 10

        payload = SignIn()
        resultado = await run_hooks(registro, "user.sign_in", "before", payload)

        assert resultado is payload

    @pytest.mark.anyio
    async def test_el_hook_corta_fuera_del_horario(self):
        from hexcore.darwin.application.hooks import run_hooks

        registro = PluginRegistry([HorarioPermitidoPlugin(desde=8, hasta=20)])
        registro.validate()

        class SignIn:
            hora = 3

        with pytest.raises(FueraDeHorarioError):
            await run_hooks(registro, "user.sign_in", "before", SignIn())

    @pytest.mark.anyio
    async def test_devolver_none_no_cambia_el_payload(self):
        """
        Es lo que hace la mayoría de los hooks, que sólo observan. Devolver un valor lo
        **reemplaza** para el hook siguiente y para el handler.
        """
        from hexcore.darwin.application.hooks import run_hooks

        registro = PluginRegistry([HorarioPermitidoPlugin()])
        registro.validate()

        payload = object()
        assert await run_hooks(registro, "cualquier.cosa", "after", payload) is payload

    @pytest.mark.anyio
    async def test_el_comodin_alcanza_cualquier_accion(self):
        from hexcore.darwin.application.hooks import run_hooks

        plugin = HorarioPermitidoPlugin()
        registro = PluginRegistry([plugin])
        registro.validate()

        await run_hooks(registro, "user.sign_in", "after", object())
        await run_hooks(registro, "session.refresh", "after", object())

        assert len(plugin.vistos) == 2

    @pytest.mark.anyio
    async def test_short_circuit_propaga_para_que_el_llamador_decida(self):
        """
        `ShortCircuit` no es un error: es el mecanismo con el que un plugin responde por su
        cuenta. `run_hooks` lo deja propagar tal cual, y quien llama decide qué significa.
        """
        from hexcore.darwin.application.hooks import run_hooks

        registro = PluginRegistry([CachePlugin("desde-cache")])
        registro.validate()

        with pytest.raises(ShortCircuit) as excinfo:
            await run_hooks(registro, "user.sign_in", "before", object())

        assert excinfo.value.result == "desde-cache"

    @pytest.mark.anyio
    async def test_un_hook_que_explota_no_se_traga(self):
        """
        El plugin falla **cerrando**. Tragar la excepción dejaría que un hook de autorización
        que explota se lea como un hook que autorizó.
        """
        from hexcore.darwin.application.hooks import run_hooks

        class Roto(DarwinPlugin):
            name = "roto"

            def hooks(self) -> t.Sequence[HookBinding]:
                return [
                    HookBinding(action="*", phase="before", handler=self._explotar)
                ]

            async def _explotar(self, payload: t.Any) -> t.Any:
                raise ValueError("algo salió mal")

        registro = PluginRegistry([Roto()])
        registro.validate()

        with pytest.raises(RuntimeError) as excinfo:
            await run_hooks(registro, "user.sign_in", "before", object())

        # El error nombra al plugin: sin eso, un traceback adentro de una cadena de hooks no
        # dice cuál de los plugins cableados lo produjo.
        assert "roto" in str(excinfo.value)


class TestOrden:
    def test_requires_manda_sobre_priority(self):
        """
        El orden es topológico por `requires`, con `(priority, orden de registro)` de desempate
        entre plugins sin relación. Determinista a propósito: si dependiera del hash de un set,
        el mismo cableado daría cadenas de hooks distintas entre corridas.
        """

        class Base(DarwinPlugin):
            name = "base"
            priority = 900

        class Encima(DarwinPlugin):
            name = "encima"
            priority = 1
            requires = ("base",)

        registro = PluginRegistry([Encima(), Base()])
        registro.validate()

        assert registro.names == ("base", "encima")

    def test_un_ciclo_se_rechaza(self):
        class A(DarwinPlugin):
            name = "a"
            requires = ("b",)

        class B(DarwinPlugin):
            name = "b"
            requires = ("a",)

        registro = PluginRegistry([A(), B()])

        with pytest.raises(PluginError, match="ciclo"):
            registro.validate()

    def test_dos_plugins_con_el_mismo_nombre_se_rechazan(self):
        """
        Quedarse con uno en silencio haría que qué plugin corre dependa del orden de
        importación.
        """

        class Uno(DarwinPlugin):
            name = "repetido"

        class Otro(DarwinPlugin):
            name = "repetido"

        with pytest.raises(PluginError) as excinfo:
            PluginRegistry([Uno(), Otro()])

        assert "repetido" in str(excinfo.value)


class TestAcciones:
    def test_la_accion_se_deriva_del_nombre_de_la_clase(self):
        class SignOutEverywhere:
            pass

        assert action_of(SignOutEverywhere) == "sign_out_everywhere"

    def test_identity_action_la_declara_explicitamente(self):
        """
        Declararla la convierte en el contrato público que es: sin el decorador, renombrar el
        comando rompería en silencio los hooks de todos los plugins.
        """

        @identity_action("user.sign_in")
        class SignIn:
            pass

        assert action_of(SignIn) == "user.sign_in"

    def test_el_decorador_devuelve_la_misma_clase(self):
        """Un decorador que degrada lo que decora no sirve en una API pública."""

        @identity_action("x.y")
        class Comando:
            campo: str = "valor"

        assert Comando().campo == "valor"


class TestCableado:
    def test_configure_identity_acepta_el_plugin(self):
        pytest.importorskip("joserfc")
        pytest.importorskip("argon2")
        from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

        reset_identity()
        try:
            contenedor = configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key="k" * 48),
                plugins=[HorarioPermitidoPlugin()],
            )

            assert contenedor.plugins.names == ("horario_permitido",)
        finally:
            reset_identity()

    def test_el_mapa_de_excepciones_del_plugin_llega_al_registro(self):
        registro = PluginRegistry([HorarioPermitidoPlugin()])
        registro.validate()

        mapas = [p.exception_status_map() for p in registro.plugins]

        assert {FueraDeHorarioError: 403} in mapas
