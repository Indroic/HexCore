"""
Darwin Fase 8: el sistema de plugins.

Tres grupos:

1. **El registro valida al cablear.** Nombre duplicado, `requires` inexistente, ciclo y
   conflicto de tablas: cada uno su error, nombrando al culpable. Descubrir cualquiera de
   ellos en el primer request de producción ya llegó tarde.
2. **El orden es determinista.** Topológico por `requires`, con `(priority, orden de registro)`
   como desempate. Si dependiera del hash de un set, el mismo cableado daría cadenas de hooks
   distintas entre corridas.
3. **Los hooks se comportan.** Encadenan el payload, `ShortCircuit` en `before` saltea el
   handler **y los `before` que quedaban**, en `after` reemplaza el resultado, una excepción
   cualquiera **propaga** (falla cerrando), los específicos corren antes que los de comodín, y
   `hooks_for` está memoizado.
"""
from __future__ import annotations

import typing as t

import pytest

from hexcore.darwin.application.hooks import HookMiddleware
from hexcore.darwin.application.plugins import PluginError, PluginRegistry
from hexcore.darwin.plugins.magic_link import MagicLinkPlugin
from hexcore.darwin.domain.plugins import (
    DarwinPlugin,
    HookBinding,
    ShortCircuit,
    action_of,
    hook_matches,
    identity_action,
)
from hexcore.domain.cqrs.commands import Command


@pytest.fixture
def anyio_backend():
    return "asyncio"


@identity_action("user.sign_in")
class Entrar(Command):
    email: str = "a@b.c"


class SinAccionDeclarada(Command):
    pass


def _plugin(
    nombre: str,
    *,
    requiere: tuple[str, ...] = (),
    prioridad: int = 100,
    tablas: dict[str, type] | None = None,
    hooks: t.Sequence[HookBinding] = (),
) -> DarwinPlugin:
    """Fabrica un plugin mínimo, para no repetir doce declaraciones de clase."""

    class _P(DarwinPlugin):
        name = nombre
        requires = requiere
        priority = prioridad

        def tables(self) -> t.Mapping[str, type]:
            return tablas or {}

        def hooks(self) -> t.Sequence[HookBinding]:
            return hooks

    _P.__qualname__ = f"Plugin_{nombre}"
    return _P()


# ── 1. Validación al cablear ──────────────────────────────────────────────────
def test_un_plugin_sin_nombre_se_rechaza():
    class SinNombre(DarwinPlugin):
        pass

    with pytest.raises(PluginError) as excinfo:
        PluginRegistry([SinNombre()])

    mensaje = str(excinfo.value)
    assert "SinNombre" in mensaje
    assert "name" in mensaje


def test_un_nombre_duplicado_se_rechaza():
    """
    Quedarse con uno en silencio haría que cuál plugin corre dependa del orden de importación.
    """
    registro = PluginRegistry([_plugin("dos_factores")])

    with pytest.raises(PluginError) as excinfo:
        registro.register(_plugin("dos_factores"))

    assert "dos_factores" in str(excinfo.value)


def test_un_requires_inexistente_se_rechaza():
    """
    Correría igual, sin lo que necesita, y fallaría más adelante en un lugar que no señala la
    causa.
    """
    registro = PluginRegistry([_plugin("impersonar", requiere=("auditoria",))])

    with pytest.raises(PluginError) as excinfo:
        registro.plugins

    mensaje = str(excinfo.value)
    assert "impersonar" in mensaje
    assert "auditoria" in mensaje


def test_un_ciclo_se_rechaza_nombrando_el_ciclo():
    """
    Descubierto acá es un error con los nombres; descubierto al ordenar en producción sería
    un `RecursionError` o un orden arbitrario.
    """
    registro = PluginRegistry(
        [
            _plugin("a", requiere=("b",)),
            _plugin("b", requiere=("c",)),
            _plugin("c", requiere=("a",)),
        ]
    )

    with pytest.raises(PluginError) as excinfo:
        registro.plugins

    mensaje = str(excinfo.value)
    assert "->" in mensaje
    for nombre in ("a", "b", "c"):
        assert nombre in mensaje


def test_un_plugin_que_depende_de_si_mismo_es_un_ciclo():
    registro = PluginRegistry([_plugin("ouroboros", requiere=("ouroboros",))])

    with pytest.raises(PluginError, match="ciclo"):
        registro.plugins


def test_dos_plugins_con_el_mismo_mixin_se_rechazan():
    """
    Si no, el consumidor no puede saber cuál mixin está componiendo — y el diff de su
    migración dependería del orden de importación.
    """

    class MiMixin:
        pass

    registro = PluginRegistry(
        [
            _plugin("uno", tablas={"TwoFactorMixin": MiMixin}),
            _plugin("otro", tablas={"TwoFactorMixin": MiMixin}),
        ]
    )

    with pytest.raises(PluginError) as excinfo:
        registro.validate()

    mensaje = str(excinfo.value)
    assert "TwoFactorMixin" in mensaje
    assert "uno" in mensaje and "otro" in mensaje


def test_configure_identity_valida_los_plugins(monkeypatch):
    """
    Al cablear, igual que el modelo de usuario. Mismo criterio que
    `CQRSFactory._assert_enqueuer_for_background_commands`.
    """
    pytest.importorskip("joserfc")
    pytest.importorskip("argon2")
    from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

    reset_identity()
    try:
        with pytest.raises(PluginError, match="ciclo"):
            configure_identity(
                IdentityConfig(storage="sqlalchemy", secret_key="k" * 48),
                plugins=PluginRegistry(
                    [_plugin("x", requiere=("y",)), _plugin("y", requiere=("x",))]
                ),
            )
    finally:
        reset_identity()


# ── PluginRegistry.coerce ────────────────────────────────────────────────────
def test_coerce_con_lista_de_plugins():
    """Una lista de plugins se normaliza a un PluginRegistry."""
    registro = PluginRegistry.coerce([_plugin("magic_link")])

    assert isinstance(registro, PluginRegistry)
    assert registro.names == ("magic_link",)


def test_coerce_con_tupla_de_plugins():
    """Una tupla de plugins también se normaliza a un PluginRegistry."""
    registro = PluginRegistry.coerce((_plugin("magic_link"),))

    assert isinstance(registro, PluginRegistry)
    assert registro.names == ("magic_link",)


def test_coerce_con_plugin_registry_devuelve_el_mismo_objeto():
    """Un PluginRegistry existente se devuelve tal cual (identidad, no copia)."""
    original = PluginRegistry([_plugin("magic_link")])

    resultado = PluginRegistry.coerce(original)

    assert resultado is original


def test_coerce_con_none_devuelve_none():
    registro = PluginRegistry.coerce(None)

    assert registro is None


def test_coerce_rechaza_un_string():
    """Un string no es registro ni secuencia; tiene que levantar TypeError."""
    with pytest.raises(TypeError) as excinfo:
        PluginRegistry.coerce("magic_link")

    assert "PluginRegistry" in str(excinfo.value)


def test_coerce_rechaza_un_int():
    """Un int no es registro ni secuencia; tiene que levantar TypeError."""
    with pytest.raises(TypeError) as excinfo:
        PluginRegistry.coerce(42)

    assert "PluginRegistry" in str(excinfo.value)


def test_coerce_rechaza_un_set():
    """
    Un set no tiene orden, y el orden decide el orden de los hooks. Rechazarlo es
    deliberado: aceptar un set cambiaría un error ruidoso por un bug silencioso de
    ordenamiento.
    """
    with pytest.raises(TypeError) as excinfo:
        PluginRegistry.coerce({MagicLinkPlugin()})

    assert "PluginRegistry" in str(excinfo.value)


def test_coerce_rechaza_un_generador():
    """
    Un generador se consume una sola vez, y el registro lo recorre más de una vez.
    Rechazarlo es deliberado: aceptarlo haría que el segundo recorrido vea cero
    plugins sin ningún error.
    """
    generador = (MagicLinkPlugin() for _ in [1])

    with pytest.raises(TypeError) as excinfo:
        PluginRegistry.coerce(generador)

    assert "PluginRegistry" in str(excinfo.value)


def test_coerce_rechaza_clase_sin_instanciar():
    """
    Tiene que levantar con un mensaje que hable de instanciar: el consumidor casi
    siempre se olvidó del paréntesis, y el mensaje genérico lo mandaría a revisar el
    tipo de la lista en vez del lugar donde falta.
    """
    with pytest.raises(TypeError) as excinfo:
        PluginRegistry.coerce([MagicLinkPlugin])

    mensaje = str(excinfo.value)
    assert "instancia" in mensaje.lower() or "instanciar" in mensaje.lower()


def test_coerce_rechaza_elemento_que_no_es_darwin_plugin():
    """
    El mensaje tiene que nombrar la posición y el tipo: sin eso, el consumidor no
    puede encontrar el objeto que falla en una lista larga.
    """
    with pytest.raises(TypeError) as excinfo:
        PluginRegistry.coerce([object()])

    mensaje = str(excinfo.value)
    assert "plugins=" in mensaje
    assert "DarwinPlugin" in mensaje


# ── Integración: configure_identity con plugins= ────────────────────────────
#
# `configure_identity` deja un contenedor **global**, así que todo test que la llame lo
# resetea antes y después, con `finally` para que un assert que falla no le filtre el
# cableado al test siguiente. Es la convención de
# `test_configure_identity_valida_los_plugins`, unas líneas más arriba.
def test_configure_identity_con_lista_de_plugins():
    pytest.importorskip("joserfc")
    pytest.importorskip("argon2")
    from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

    reset_identity()
    try:
        contenedor = configure_identity(
            IdentityConfig(storage="sqlalchemy", secret_key="k" * 48),
            plugins=[MagicLinkPlugin()],
        )

        assert contenedor.plugins.names == ("magic_link",)
    finally:
        reset_identity()


def test_configure_identity_con_plugin_registry():
    """
    Si ya pasaste un PluginRegistry, el contenedor tiene que recibir exactamente el
    mismo objeto — no una copia.
    """
    pytest.importorskip("joserfc")
    pytest.importorskip("argon2")
    from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

    reset_identity()
    try:
        registro = PluginRegistry([MagicLinkPlugin()])
        contenedor = configure_identity(
            IdentityConfig(storage="sqlalchemy", secret_key="k" * 48),
            plugins=registro,
        )

        assert contenedor.plugins is registro
    finally:
        reset_identity()


def test_configure_identity_sin_plugins_da_registro_vacio():
    pytest.importorskip("joserfc")
    pytest.importorskip("argon2")
    from hexcore.darwin import IdentityConfig, configure_identity, reset_identity

    reset_identity()
    try:
        contenedor = configure_identity(
            IdentityConfig(storage="sqlalchemy", secret_key="k" * 48)
        )

        assert contenedor.plugins.names == ()
    finally:
        reset_identity()


def test_identity_container_con_lista_de_plugins():
    """
    El contenedor construido directo también normaliza: `configure_identity` no es la
    única puerta de entrada, y quien arma un `IdentityContainer` en un test merece el
    mismo trato.

    No llama a `configure_identity`, así que no toca el contenedor global y no hace
    falta resetear nada.
    """
    pytest.importorskip("joserfc")
    pytest.importorskip("argon2")
    from hexcore.darwin import IdentityConfig, IdentityContainer

    cfg = IdentityConfig(storage="sqlalchemy", secret_key="k" * 48)
    contenedor = IdentityContainer(cfg, plugins=[MagicLinkPlugin()])

    assert contenedor.plugins.names == ("magic_link",)


# ── 2. Orden determinista ─────────────────────────────────────────────────────
def test_el_orden_respeta_requires():
    """Un plugin corre **después** de todo lo que declara requerir."""
    registro = PluginRegistry(
        [
            _plugin("impersonar", requiere=("auditoria",)),
            _plugin("auditoria"),
        ]
    )

    nombres = registro.names

    assert nombres.index("auditoria") < nombres.index("impersonar")


def test_el_orden_es_transitivo():
    registro = PluginRegistry(
        [
            _plugin("c", requiere=("b",)),
            _plugin("b", requiere=("a",)),
            _plugin("a"),
        ]
    )

    assert registro.names == ("a", "b", "c")


def test_la_prioridad_desempata_entre_independientes():
    registro = PluginRegistry(
        [
            _plugin("tarde", prioridad=900),
            _plugin("temprano", prioridad=10),
        ]
    )

    assert registro.names == ("temprano", "tarde")


def test_el_orden_de_registro_desempata_a_igual_prioridad():
    """
    Determinista a propósito: si dependiera del hash de un set, el mismo cableado daría
    cadenas de hooks distintas entre corridas.
    """
    registro = PluginRegistry(
        [_plugin("primero"), _plugin("segundo"), _plugin("tercero")]
    )

    assert registro.names == ("primero", "segundo", "tercero")


def test_el_orden_se_cachea_y_se_invalida_al_registrar():
    registro = PluginRegistry([_plugin("a")])
    assert registro.names == ("a",)

    registro.register(_plugin("b", prioridad=1))

    assert registro.names == ("b", "a")


def test_un_registro_vacio_es_truthy():
    """
    Sin `__bool__` explícito, un registro sin plugins es falsy y un `if registro:` lo
    descarta. Es el mismo defecto que `InMemoryTaskEnqueuer` documenta.
    """
    assert bool(PluginRegistry()) is True
    assert len(PluginRegistry()) == 0


# ── Agregación de aportes ─────────────────────────────────────────────────────
def test_los_aportes_se_agregan_en_orden_de_plugin():
    class A:
        pass

    class B:
        pass

    registro = PluginRegistry(
        [
            _plugin("segundo", prioridad=200, tablas={"B": B}),
            _plugin("primero", prioridad=100, tablas={"A": A}),
        ]
    )

    assert list(registro.tables()) == ["A", "B"]


def test_un_plugin_que_no_aporta_nada_es_valido():
    """
    Todos los métodos de aporte son **concretos** y devuelven vacío: un plugin declara nada
    más lo que aporta, y agregar un punto de extensión nuevo no rompe a ninguno existente.
    """
    registro = PluginRegistry([_plugin("vacio")])
    registro.validate()

    assert registro.tables() == {}
    assert registro.routers() == []
    assert registro.hooks() == []
    assert registro.startup_steps() == []


# ── Acciones ──────────────────────────────────────────────────────────────────
def test_la_accion_declarada_gana():
    assert action_of(Entrar()) == "user.sign_in"
    assert action_of(Entrar) == "user.sign_in"


def test_sin_declarar_se_deriva_de_la_clase():
    """
    Alcanza para lo que shippea Darwin, pero ata el hook al nombre de la clase: renombrar el
    comando rompería en silencio los hooks de todos los plugins. De ahí `@identity_action`.
    """
    assert action_of(SinAccionDeclarada()) == "sin_accion_declarada"


@pytest.mark.parametrize(
    "patron, accion, esperado",
    [
        ("user.sign_in", "user.sign_in", True),
        ("user.sign_in", "user.sign_up", False),
        ("user.*", "user.sign_in", True),
        ("*", "cualquier.cosa", True),
        ("session.*", "user.sign_in", False),
        # Sensible a mayúsculas: `fnmatchcase`. Un patrón que matchea de más es un hook que
        # corre donde nadie lo esperaba.
        ("User.*", "user.sign_in", False),
    ],
)
def test_los_comodines_matchean_lo_que_deben(patron, accion, esperado):
    binding = HookBinding(action=patron, phase="before", handler=_nada)
    assert hook_matches(binding, accion) is esperado


async def _nada(payload: t.Any) -> None:
    return None


# ── 3. Los hooks ──────────────────────────────────────────────────────────────
async def _handler_identidad(mensaje: t.Any) -> t.Any:
    return mensaje


def _middleware(*hooks: HookBinding) -> HookMiddleware:
    return HookMiddleware(PluginRegistry([_plugin("p", hooks=hooks)]))


@pytest.mark.anyio
async def test_un_hook_que_devuelve_none_no_cambia_nada():
    """Lo que hace la mayoría: sólo observa."""
    vistos: list[t.Any] = []

    async def observar(payload: t.Any) -> None:
        vistos.append(payload)
        return None

    comando = Entrar(email="ana@b.c")
    mw = _middleware(
        HookBinding(action="user.sign_in", phase="before", handler=observar)
    )

    resultado = await mw.handle(comando, _handler_identidad)

    assert resultado is comando
    assert vistos == [comando]


@pytest.mark.anyio
async def test_un_hook_puede_reemplazar_el_payload():
    """
    Los mensajes son `frozen`, así que el hook **no puede** mutar: tiene que devolver una
    instancia nueva. Es la diferencia deliberada con los hooks que mutan un `ctx` compartido.
    """

    async def normalizar(comando: Entrar) -> Entrar:
        return comando.model_copy(update={"email": comando.email.lower()})

    mw = _middleware(
        HookBinding(action="user.sign_in", phase="before", handler=normalizar)
    )

    resultado = await mw.handle(Entrar(email="ANA@B.C"), _handler_identidad)

    assert resultado.email == "ana@b.c"


@pytest.mark.anyio
async def test_los_hooks_encadenan_el_payload():
    """Encadenar y no acumular: así un hook puede refinar lo que hizo el anterior."""

    async def uno(c: Entrar) -> Entrar:
        return c.model_copy(update={"email": c.email + "+1"})

    async def dos(c: Entrar) -> Entrar:
        return c.model_copy(update={"email": c.email + "+2"})

    mw = _middleware(
        HookBinding(action="user.sign_in", phase="before", handler=uno, priority=10),
        HookBinding(action="user.sign_in", phase="before", handler=dos, priority=20),
    )

    resultado = await mw.handle(Entrar(email="a"), _handler_identidad)

    assert resultado.email == "a+1+2"


@pytest.mark.anyio
async def test_short_circuit_en_before_saltea_el_handler():
    """
    El mecanismo con el que un plugin responde por su cuenta: 2FA que exige el segundo factor,
    un bloqueo por país, una cuota agotada.
    """
    corrio = False

    async def handler(mensaje: t.Any) -> str:
        nonlocal corrio
        corrio = True
        return "el handler corrió"  # pragma: no cover

    async def cortar(payload: t.Any) -> None:
        raise ShortCircuit("hace falta 2FA")

    mw = _middleware(
        HookBinding(action="user.sign_in", phase="before", handler=cortar)
    )

    resultado = await mw.handle(Entrar(), handler)

    assert resultado == "hace falta 2FA"
    assert corrio is False


@pytest.mark.anyio
async def test_short_circuit_en_before_saltea_los_before_que_quedaban():
    """
    Los hooks siguientes esperaban un payload que ya no se va a procesar, y correrlos sería
    trabajo sobre una decisión ya tomada.
    """
    corridos: list[str] = []

    async def primero(payload: t.Any) -> None:
        corridos.append("primero")
        raise ShortCircuit("cortado")

    async def segundo(payload: t.Any) -> None:
        corridos.append("segundo")  # pragma: no cover
        return None

    mw = _middleware(
        HookBinding(action="user.sign_in", phase="before", handler=primero, priority=10),
        HookBinding(action="user.sign_in", phase="before", handler=segundo, priority=20),
    )

    await mw.handle(Entrar(), _handler_identidad)

    assert corridos == ["primero"]


@pytest.mark.anyio
async def test_short_circuit_en_after_reemplaza_el_resultado():
    """
    En `after` el handler **ya corrió**: cortocircuitar reemplaza el resultado, no cancela el
    efecto. Un plugin que quiera impedir la operación tiene que hacerlo en `before`.
    """
    corrio = False

    async def handler(mensaje: t.Any) -> str:
        nonlocal corrio
        corrio = True
        return "original"

    async def reemplazar(resultado: t.Any) -> None:
        raise ShortCircuit("reemplazado")

    mw = _middleware(
        HookBinding(action="user.sign_in", phase="after", handler=reemplazar)
    )

    resultado = await mw.handle(Entrar(), handler)

    assert resultado == "reemplazado"
    assert corrio is True


@pytest.mark.anyio
async def test_los_hooks_after_ven_el_resultado_del_handler():
    async def handler(mensaje: t.Any) -> str:
        return "del handler"

    vistos: list[t.Any] = []

    async def observar(resultado: t.Any) -> None:
        vistos.append(resultado)
        return None

    mw = _middleware(
        HookBinding(action="user.sign_in", phase="after", handler=observar)
    )

    await mw.handle(Entrar(), handler)

    assert vistos == ["del handler"]


@pytest.mark.anyio
async def test_una_excepcion_cualquiera_propaga():
    """
    **Falla cerrando.** Tragarla dejaría que un hook de autorización que explota se lea como
    uno que autorizó — el peor modo de falla posible para un sistema de plugins de auth.
    """

    async def explotar(payload: t.Any) -> None:
        raise ValueError("el hook tiene un bug")

    mw = _middleware(
        HookBinding(action="user.sign_in", phase="before", handler=explotar, plugin="p")
    )

    with pytest.raises(RuntimeError) as excinfo:
        await mw.handle(Entrar(), _handler_identidad)

    mensaje = str(excinfo.value)
    assert "'p'" in mensaje
    assert "before" in mensaje
    assert "user.sign_in" in mensaje


@pytest.mark.anyio
async def test_un_hook_de_otra_accion_no_corre():
    corrio = False

    async def ajeno(payload: t.Any) -> None:
        nonlocal corrio
        corrio = True  # pragma: no cover
        return None

    mw = _middleware(
        HookBinding(action="session.revoke", phase="before", handler=ajeno)
    )

    await mw.handle(Entrar(), _handler_identidad)

    assert corrio is False


# ── Orden y memoización de los hooks ──────────────────────────────────────────
def test_los_especificos_corren_antes_que_los_comodines():
    """
    Un hook de auditoría con `"*"` quiere ver el payload final, no el que llegó antes de que
    los específicos lo ajustaran.
    """
    registro = PluginRegistry(
        [
            _plugin(
                "p",
                hooks=[
                    HookBinding(action="*", phase="before", handler=_nada, priority=1),
                    HookBinding(
                        action="user.sign_in",
                        phase="before",
                        handler=_nada,
                        priority=999,
                    ),
                ],
            )
        ]
    )

    orden = registro.hooks_for("user.sign_in", "before")

    # El específico va primero **aunque su prioridad sea mucho mayor**: el comodín se ordena
    # después por ser comodín, no por prioridad.
    assert [b.action for b in orden] == ["user.sign_in", "*"]


def test_las_fases_no_se_mezclan():
    registro = PluginRegistry(
        [
            _plugin(
                "p",
                hooks=[
                    HookBinding(action="user.*", phase="before", handler=_nada),
                    HookBinding(action="user.*", phase="after", handler=_nada),
                ],
            )
        ]
    )

    assert len(registro.hooks_for("user.sign_in", "before")) == 1
    assert len(registro.hooks_for("user.sign_in", "after")) == 1


def test_hooks_for_esta_memoizado():
    """
    Corre en cada mensaje: sin cache, cada uno pagaría un `fnmatch` por hook registrado.

    Se cuenta la construcción de la lista espiando `hooks()`, que es lo que `hooks_for`
    recorre.
    """
    registro = PluginRegistry(
        [_plugin("p", hooks=[HookBinding(action="*", phase="before", handler=_nada)])]
    )
    llamadas = 0
    original = registro.hooks

    def espia() -> t.Any:
        nonlocal llamadas
        llamadas += 1
        return original()

    registro.hooks = espia  # type: ignore[method-assign]

    registro.hooks_for("user.sign_in", "before")
    primera = llamadas
    registro.hooks_for("user.sign_in", "before")
    registro.hooks_for("user.sign_in", "before")

    assert llamadas == primera, "el segundo y el tercer acceso salieron del cache"


def test_el_cache_de_hooks_se_invalida_al_registrar():
    registro = PluginRegistry(
        [_plugin("a", hooks=[HookBinding(action="*", phase="before", handler=_nada)])]
    )
    assert len(registro.hooks_for("x", "before")) == 1

    registro.register(
        _plugin("b", hooks=[HookBinding(action="*", phase="before", handler=_nada)])
    )

    assert len(registro.hooks_for("x", "before")) == 2


def test_el_registro_estampa_el_nombre_del_plugin_en_sus_hooks():
    """
    Se estampa acá y no lo declara el hook: a mano sería un dato duplicado que se puede
    desincronizar, y su único uso es aparecer en los errores.
    """
    registro = PluginRegistry(
        [_plugin("dos_factores", hooks=[HookBinding(action="*", phase="before", handler=_nada)])]
    )

    assert registro.hooks()[0].plugin == "dos_factores"


# ── El pipeline real ──────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_el_hook_middleware_se_compone_con_el_pipeline_real():
    """
    No hay mecanismo nuevo: `HookMiddleware` es un `AbstractMiddleware` y entra en el
    `MiddlewarePipeline` como cualquier otro.
    """
    from hexcore.application.cqrs.pipeline import MiddlewarePipeline
    from hexcore.domain.cqrs.handlers import AbstractCommandHandler
    from hexcore.testing import build_test_buses

    vistos: list[str] = []

    class Handler(AbstractCommandHandler[Entrar, str]):
        async def handle(self, command: Entrar) -> str:
            vistos.append(command.email)
            return "ok"

    async def normalizar(c: Entrar) -> Entrar:
        return c.model_copy(update={"email": c.email.lower()})

    registro = PluginRegistry(
        [
            _plugin(
                "p",
                hooks=[
                    HookBinding(
                        action="user.sign_in", phase="before", handler=normalizar
                    )
                ],
            )
        ]
    )

    from hexcore.application.cqrs.in_memory_buses import InMemoryCommandBus

    buses = build_test_buses()
    buses.registry.register_command_handler(Entrar, Handler())
    bus = InMemoryCommandBus(
        registry=buses.registry,
        pipeline=MiddlewarePipeline([HookMiddleware(registro)]),
    )

    resultado = await bus.dispatch(Entrar(email="ANA@B.C"))

    assert resultado == "ok"
    assert vistos == ["ana@b.c"], "el hook corrió antes del handler"
