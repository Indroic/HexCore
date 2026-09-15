"""
Darwin Fase 4: las tres capas de revocación.

El test central es **la regresión de `MemoryCache`**: verificado que `MemoryCache.set()` ignora
su parámetro `expire` y nunca desaloja. Si la denylist delegara el vencimiento al TTL del
backend, una revocación sería *permanente* con el backend por defecto y la lista crecería sin
techo. Por eso el vencimiento va dentro del valor — igual que `rate_limit` guarda su `reset_at`.

El otro invariante que se fija acá: **falla cerrando**. Al revés que `rate_limit`, y a
propósito: dejar pasar una petición sin limitar es una molestia, dejar pasar un token revocado
es la vulnerabilidad que este módulo existe para evitar.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hexcore.darwin import (
    CacheRevocationList,
    FixedClock,
    GenerationGuard,
    TokenRevokedError,
)
from hexcore.infrastructure.cache import ICache
from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def cache() -> MemoryCache:
    return MemoryCache()


@pytest.fixture
def lista(cache, reloj) -> CacheRevocationList:
    return CacheRevocationList(cache=cache, clock=reloj)


class CacheRoto(ICache):
    """Backend que siempre falla, para ejercer la política de error."""

    async def get(self, key: str) -> t.Any:
        raise ConnectionError("redis caído")

    def set(self, key: str, value: t.Any, expire: int = 3600) -> t.Any:
        raise ConnectionError("redis caído")

    def delete(self, key: str) -> t.Any:
        raise ConnectionError("redis caído")


# ── Semántica básica ──────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_una_sesion_no_revocada_pasa(lista):
    """Se permite si no está en la lista. Es correcto porque el `until` cubre la vida del token."""
    assert await lista.is_revoked(uuid4()) is False


@pytest.mark.anyio
async def test_una_sesion_revocada_no_pasa(lista):
    sid = uuid4()
    await lista.revoke(sid, until=AHORA + timedelta(minutes=5))

    assert await lista.is_revoked(sid) is True


@pytest.mark.anyio
async def test_revocar_una_no_afecta_a_las_otras(lista):
    revocada, viva = uuid4(), uuid4()
    await lista.revoke(revocada, until=AHORA + timedelta(minutes=5))

    assert await lista.is_revoked(revocada) is True
    assert await lista.is_revoked(viva) is False


@pytest.mark.anyio
async def test_clear_deshace_la_revocacion(lista):
    sid = uuid4()
    await lista.revoke(sid, until=AHORA + timedelta(minutes=5))
    await lista.clear(sid)

    assert await lista.is_revoked(sid) is False


# ── La regresión de MemoryCache ───────────────────────────────────────────────
@pytest.mark.anyio
async def test_memory_cache_ignora_expire():
    """
    Documenta el hecho del que depende todo este diseño, en vez de asumirlo.

    `MemoryCache.set()` no mira `expire` y nunca desaloja.
    """
    cache = MemoryCache()
    await cache.set("k", {"v": 1}, expire=1)

    assert await cache.get("k") == {"v": 1}


@pytest.mark.anyio
async def test_la_revocacion_vence_por_el_valor_no_por_el_ttl(lista, reloj, cache):
    """
    **El test que fija la decisión de diseño.**

    Si la implementación confiara en el TTL del backend, esto fallaría: con `MemoryCache` la
    entrada seguiría ahí y la sesión quedaría revocada para siempre.
    """
    sid = uuid4()
    await lista.revoke(sid, until=AHORA + timedelta(minutes=5))
    assert await lista.is_revoked(sid) is True

    reloj.advance(minutes=6)

    assert await lista.is_revoked(sid) is False, (
        "la revocación no venció: la implementación está confiando en el TTL del backend, "
        "y MemoryCache lo ignora"
    )
    # La entrada sigue físicamente en el cache — es exactamente el punto.
    assert any(str(sid) in k for k in cache.cache)


@pytest.mark.anyio
async def test_una_entrada_sin_until_no_revoca(lista, cache):
    """Un valor corrupto o de otra versión no debe desloguear a nadie."""
    sid = uuid4()
    await cache.set(f"darwin:revoked:{sid}", {"otra": "forma"})

    assert await lista.is_revoked(sid) is False


# ── Falla cerrando ────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_por_defecto_falla_cerrando(reloj):
    """
    Al revés que `rate_limit`. Dejar pasar una petición sin limitar es una molestia; dejar
    pasar un token revocado es la vulnerabilidad que esta clase existe para evitar.
    """
    lista = CacheRevocationList(cache=CacheRoto(), clock=reloj)

    with pytest.raises(TokenRevokedError):
        await lista.is_revoked(uuid4())


@pytest.mark.anyio
async def test_allow_explicito_deja_pasar(reloj, caplog):
    """Existe sólo para un despliegue que prefiera disponibilidad y lo declare a mano."""
    lista = CacheRevocationList(
        cache=CacheRoto(), clock=reloj, on_cache_error="allow"
    )

    assert await lista.is_revoked(uuid4()) is False


@pytest.mark.anyio
async def test_el_fallo_cerrado_se_loguea_como_critico(reloj, caplog):
    """
    Mientras el backend esté caído, **ninguna** sesión valida. Eso tiene que estar en los logs
    con severidad crítica o nadie entiende por qué la app rechaza todo.
    """
    import logging

    lista = CacheRevocationList(cache=CacheRoto(), clock=reloj)

    with caplog.at_level(logging.CRITICAL, logger="hexcore.darwin.revocation"):
        with pytest.raises(TokenRevokedError):
            await lista.is_revoked(uuid4())

    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


# ── Capa 3: generación ────────────────────────────────────────────────────────
class UsuarioFalso:
    def __init__(self, generation: int = 0) -> None:
        self.token_generation = generation


class RepoUsuariosFalso:
    def __init__(self, generation: int = 0) -> None:
        self.usuario = UsuarioFalso(generation)
        self.consultas = 0

    async def get_by_id(self, user_id):
        self.consultas += 1
        return self.usuario


@pytest.fixture
def usuarios() -> RepoUsuariosFalso:
    return RepoUsuariosFalso()


@pytest.fixture
def guard(usuarios, cache, reloj) -> GenerationGuard:
    return GenerationGuard(users=usuarios, cache=cache, clock=reloj)


@pytest.mark.anyio
async def test_un_token_de_la_generacion_actual_es_valido(guard):
    assert await guard.is_stale(uuid4(), 0) is False


@pytest.mark.anyio
async def test_un_token_de_una_generacion_anterior_es_stale(usuarios, cache, reloj):
    usuarios.usuario.token_generation = 3
    guard = GenerationGuard(users=usuarios, cache=cache, clock=reloj)

    assert await guard.is_stale(uuid4(), 2) is True
    assert await guard.is_stale(uuid4(), 3) is False


@pytest.mark.anyio
async def test_un_token_del_futuro_no_es_stale(usuarios, guard):
    """
    Se compara con `<`, no con `!=`. Un `gen` mayor que el del usuario es imposible salvo bug de
    emisión o base restaurada de un backup — y tratarlo como stale deslogearía a todo el mundo
    tras un restore.
    """
    assert await guard.is_stale(uuid4(), 99) is False


@pytest.mark.anyio
async def test_la_generacion_se_cachea(guard, usuarios):
    """El camino caliente no debe consultar la base en cada petición."""
    uid = uuid4()

    for _ in range(10):
        await guard.current_generation(uid)

    assert usuarios.consultas == 1


@pytest.mark.anyio
async def test_el_cache_de_generacion_vence_por_el_valor(guard, usuarios, reloj):
    """Mismo motivo que la denylist: `MemoryCache` ignora `expire`."""
    uid = uuid4()
    await guard.current_generation(uid)
    assert usuarios.consultas == 1

    reloj.advance(seconds=61)
    await guard.current_generation(uid)

    assert usuarios.consultas == 2


@pytest.mark.anyio
async def test_invalidate_cache_fuerza_una_lectura(guard, usuarios):
    """
    Se llama junto con `bump_token_generation`. Sin esto, la ventana del cache sigue aceptando
    tokens viejos después de un "cerrar todas las sesiones" — justo el flujo donde el usuario
    espera efecto inmediato.
    """
    uid = uuid4()
    await guard.current_generation(uid)
    usuarios.usuario.token_generation = 5

    await guard.invalidate_cache(uid)

    assert await guard.current_generation(uid) == 5
    assert usuarios.consultas == 2


@pytest.mark.anyio
async def test_el_guard_tambien_falla_cerrando(usuarios, reloj):
    guard = GenerationGuard(users=usuarios, cache=CacheRoto(), clock=reloj)

    with pytest.raises(TokenRevokedError):
        await guard.current_generation(uuid4())


@pytest.mark.anyio
async def test_un_usuario_inexistente_da_generacion_cero(cache, reloj):
    """
    No lanza: los ids llegan desde el token, o sea desde afuera. Un usuario borrado no debería
    tumbar el request — y devolver 0 hace que cualquier token con `gen >= 0` no sea stale por
    esta capa, lo cual está bien porque las otras dos ya lo cubren.
    """

    class RepoVacio:
        async def get_by_id(self, user_id):
            return None

    guard = GenerationGuard(users=RepoVacio(), cache=cache, clock=reloj)

    assert await guard.current_generation(uuid4()) == 0
