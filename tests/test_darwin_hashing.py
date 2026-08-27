"""
Darwin Fase 4: hasheo de contraseñas y de tokens.

Dos primitivas y la diferencia importa: Argon2id para contraseñas (entropía baja, hay que
frenar un diccionario), SHA-256 para tokens (aleatorios de 256 bits, no hay diccionario del que
defenderse y un KDF lento sólo cuesta latencia por petición).

El test que más importa es el de **timing**: sin la contraseña señuelo, un login contra un mail
inexistente responde en microsegundos y uno contra un mail real en decenas de milisegundos, y
esa diferencia revela qué mails están registrados sin adivinar ni una contraseña.
"""
from __future__ import annotations

import pytest

pytest.importorskip("argon2")

from hexcore.darwin import (  # noqa: E402
    Argon2PasswordHasher,
    compare_hashes,
    generate_numeric_code,
    generate_token,
    hash_token,
)


@pytest.fixture
def hasher() -> Argon2PasswordHasher:
    return Argon2PasswordHasher()


# ── Contraseñas ───────────────────────────────────────────────────────────────
def test_hashear_y_verificar(hasher):
    guardado = hasher.hash("correohorsebatterystaple")

    assert hasher.verify("correohorsebatterystaple", guardado) is True
    assert hasher.verify("otra-cosa", guardado) is False


def test_el_hash_es_argon2id(hasher):
    """Argon2id, no Argon2i ni Argon2d: es la variante que recomienda el RFC 9106."""
    assert hasher.hash("x").startswith("$argon2id$")


def test_dos_hashes_de_la_misma_clave_difieren(hasher):
    """Salt por hash. Sin eso, dos usuarios con la misma contraseña tienen el mismo hash."""
    assert hasher.hash("misma") != hasher.hash("misma")


@pytest.mark.parametrize(
    "hash_invalido", ["", "no-es-un-hash", "$argon2id$corrupto", "$1$viejo$x"]
)
def test_verify_nunca_lanza(hasher, hash_invalido):
    """
    Que devuelva `False` en vez de lanzar es deliberado: `argon2` distingue "no coincide" de
    "el hash está corrupto" con excepciones distintas, y propagarlas le daría al atacante un
    canal para distinguir esos casos — además de convertir un hash mal migrado en un 500 en
    vez de un login fallido.
    """
    assert hasher.verify("cualquiera", hash_invalido) is False


def test_needs_rehash_es_false_para_un_hash_actual(hasher):
    assert hasher.needs_rehash(hasher.hash("x")) is False


@pytest.mark.parametrize(
    "legado",
    [
        "$2b$12$abcdefghijklmnopqrstuv",
        "$2a$10$abcdefghijklmnopqrstuv",
        "$2y$12$abcdefghijklmnopqrstuv",
    ],
)
def test_needs_rehash_pide_migrar_los_bcrypt(hasher, legado):
    """
    Es la señal de migración. Sin esto, una base migrada desde bcrypt se queda en bcrypt para
    siempre: nadie vuelve a pasar por el punto donde se podría actualizar.
    """
    assert hasher.needs_rehash(legado) is True


def test_needs_rehash_pide_migrar_un_hash_vacio(hasher):
    assert hasher.needs_rehash("") is True


def test_el_costo_no_esta_hardcodeado():
    """
    Los parámetros los elige `argon2-cffi`, que los mantiene alineados con la recomendación
    vigente. Fijarlos en el código significa quedarse con los valores de hoy para siempre, y el
    costo recomendado sube con el hardware.
    """
    import inspect

    fuente = inspect.getsource(Argon2PasswordHasher.__init__)

    for parametro in ("time_cost", "memory_cost", "parallelism"):
        assert parametro not in fuente


# ── Timing: el oráculo de enumeración ─────────────────────────────────────────
def test_hash_dummy_existe_y_cuesta_lo_mismo(hasher):
    """
    **El test que cubre la enumeración de usuarios por tiempo.**

    El flujo de sign-in tiene que llamar a `hash_dummy()` en la rama "no encontré la fila".
    Acá se verifica que exista y que su costo sea del mismo orden que un hash real — no se mide
    el reloj (sería un test escamoso), se verifica que haga el trabajo: un `hash_dummy` que no
    hashee nada no igualaría nada.
    """
    import time

    inicio = time.perf_counter()
    hasher.hash_dummy()
    dummy = time.perf_counter() - inicio

    inicio = time.perf_counter()
    hasher.hash("una-contraseña-real")
    real = time.perf_counter() - inicio

    # Mismo orden de magnitud. El margen es amplio a propósito: la precisión del reloj y el
    # ruido del CI no permiten afirmar más que esto sin volverlo escamoso.
    assert dummy > 0
    assert 0.1 < (dummy / real) < 10


def test_hash_dummy_no_devuelve_nada(hasher):
    """Devolver el hash invitaría a compararlo con algo, y no hay nada con qué compararlo."""
    assert hasher.hash_dummy() is None


# ── Tokens ────────────────────────────────────────────────────────────────────
def test_hash_token_es_sha256_hex():
    """SHA-256 y no Argon2: el token es aleatorio de 256 bits, no hay diccionario que frenar."""
    resultado = hash_token("un-token")

    assert len(resultado) == 64
    assert all(c in "0123456789abcdef" for c in resultado)


def test_hash_token_es_determinista():
    """Tiene que serlo: se busca la sesión **por** el hash."""
    assert hash_token("igual") == hash_token("igual")
    assert hash_token("a") != hash_token("b")


def test_compare_hashes_compara_en_tiempo_constante():
    """
    `==` sale en el primer byte distinto, así que el tiempo filtra cuántos caracteres del
    prefijo acertó el atacante — y con eso una búsqueda exponencial se vuelve lineal.

    Se verifica que use `hmac.compare_digest` contando las llamadas, que es lo único
    determinista: medir tiempos acá sería escamoso.
    """
    import hmac

    llamadas: list[int] = []
    original = hmac.compare_digest

    def contador(a, b):
        llamadas.append(1)
        return original(a, b)

    hmac.compare_digest = contador
    try:
        assert compare_hashes("a" * 64, "a" * 64) is True
        assert compare_hashes("a" * 64, "b" * 64) is False
    finally:
        hmac.compare_digest = original

    assert len(llamadas) == 2


def test_generate_token_usa_secrets():
    """
    `random` usa Mersenne Twister, predecible tras observar suficiente salida — y para un token
    de sesión eso es una toma de cuenta.
    """
    import inspect

    from hexcore.darwin.infrastructure import hashing

    assert "secrets.token_urlsafe" in inspect.getsource(hashing.generate_token)


def test_generate_token_tiene_entropia_suficiente():
    tokens = {generate_token() for _ in range(200)}

    assert len(tokens) == 200
    # 32 bytes en base64url sin relleno.
    assert all(len(x) >= 42 for x in tokens)


def test_generate_numeric_code_respeta_el_largo():
    for digitos in (4, 6, 8):
        codigo = generate_numeric_code(digitos)
        assert len(codigo) == digitos
        assert codigo.isdigit()


def test_generate_numeric_code_rellena_con_ceros():
    """Sin `zfill`, un código chico saldría con menos dígitos y el largo delataría el valor."""
    codigos = {generate_numeric_code(6) for _ in range(500)}

    assert all(len(c) == 6 for c in codigos)


def test_generate_numeric_code_rechaza_un_espacio_ridiculo():
    with pytest.raises(ValueError, match=">= 4"):
        generate_numeric_code(3)
