"""
Claves de firma, rotación y documento JWKS.

Tres estados por clave, y el del medio es el que hace posible rotar sin desloguear a nadie:

- ``active``: firma y verifica. **Exactamente una** por vez.
- ``verify_only``: ya no firma, todavía verifica. Los tokens emitidos antes de la rotación
  siguen siendo válidos hasta que venzan.
- ``retired``: no verifica. Se retira recién cuando pasó el TTL máximo de token.

Rotar sin el estado intermedio invalida de golpe todo token en vuelo: cada sesión activa
recibe un 401 y el usuario ve un logout masivo inexplicable. Con `verify_only`, la rotación es
invisible.

⚠️ **Los verificadores incluyen los workers.** El proceso que consume la cola verifica el sobre
firmado del actor, así que el conjunto de claves tiene que ser alcanzable desde ahí. Es un
requisito de cableado, no sólo de criptografía, y es fácil de olvidar hasta que un job falla
en producción.
"""
from __future__ import annotations

import abc
import threading
import typing as t
from datetime import datetime

from hexcore.darwin.domain.exceptions import IdentityError

__all__ = [
    "KeyStatus",
    "SigningKey",
    "UnknownKeyError",
    "RetiredKeyError",
    "NoActiveKeyError",
    "AbstractKeyStore",
    "StaticKeyStore",
    "generate_signing_key",
    "jwks_document",
]

#: `Ed25519` por defecto: firmas cortas, verificación rápida y **sin parámetros que configurar
#: mal**. Con RSA el largo de clave es una decisión que alguien va a tomar mal; con Ed25519 no
#: hay decisión que tomar.
#:
#: Se usa `Ed25519` y no el `EdDSA` genérico porque **RFC 9864 deprecó ese identificador** en
#: favor del nombre de la curva. `joserfc` emite un `SecurityWarning` si se usa `EdDSA`, y
#: arrastrar un algoritmo deprecado en un módulo de auth es deuda que después cuesta migrar:
#: los tokens ya emitidos llevan el `alg` viejo en la cabecera.
DEFAULT_ALGORITHM = "Ed25519"

KeyStatus = t.Literal["active", "verify_only", "retired"]


class UnknownKeyError(IdentityError):
    """El `kid` del token no existe en el almacén."""


class RetiredKeyError(IdentityError):
    """El `kid` existe pero su clave ya está retirada."""


class NoActiveKeyError(IdentityError):
    """No hay clave `active` con la que firmar."""


class SigningKey:
    """
    Una clave de firma y su estado.

    No es un modelo pydantic a propósito: lleva material criptográfico, y un `model_dump()`
    accidental —en un log, en un mensaje de error, en un `repr` de pytest— volcaría la clave
    privada. `__repr__` la enmascara explícitamente.
    """

    __slots__ = ("kid", "algorithm", "public_key", "_private_key", "status", "created_at")

    def __init__(
        self,
        *,
        kid: str,
        algorithm: str,
        public_key: str,
        private_key: str,
        status: KeyStatus = "active",
        created_at: datetime | None = None,
    ) -> None:
        self.kid = kid
        self.algorithm = algorithm
        self.public_key = public_key
        self._private_key = private_key
        self.status = status
        self.created_at = created_at

    @property
    def private_key(self) -> str:
        return self._private_key

    @property
    def can_sign(self) -> bool:
        return self.status == "active"

    @property
    def can_verify(self) -> bool:
        """`retired` no verifica. `verify_only` sí: es el punto del estado intermedio."""
        return self.status in ("active", "verify_only")

    def __repr__(self) -> str:
        # La privada NO va en el repr. Un traceback de pytest imprime los locals.
        return (
            f"SigningKey(kid={self.kid!r}, algorithm={self.algorithm!r}, "
            f"status={self.status!r}, private_key=<oculta>)"
        )


def generate_signing_key(
    *, kid: str | None = None, algorithm: str = DEFAULT_ALGORITHM
) -> SigningKey:
    """
    Genera un par de claves nuevo, serializado como JWK.

    Args:
        kid: Identificador. Por defecto uno aleatorio — **no** un timestamp ni un contador:
            un `kid` predecible le dice al atacante cuántas claves hubo y cuándo rotaste.
        algorithm: `Ed25519` por defecto. Ver `DEFAULT_ALGORITHM`.

    Uso::

        clave = generate_signing_key()
        almacen = StaticKeyStore([clave])
    """
    import json
    import secrets

    from joserfc.jwk import OctKey, OKPKey, RSAKey

    identificador = kid or secrets.token_urlsafe(12)

    if algorithm in ("Ed25519", "Ed448"):
        par = OKPKey.generate_key(algorithm, parameters={"kid": identificador})
    elif algorithm == "EdDSA":
        # Aceptado por compatibilidad con claves ya emitidas, no recomendado (RFC 9864).
        par = OKPKey.generate_key("Ed25519", parameters={"kid": identificador})
    elif algorithm.startswith("RS") or algorithm.startswith("PS"):
        par = RSAKey.generate_key(2048, parameters={"kid": identificador})
    elif algorithm.startswith("HS"):
        par = OctKey.generate_key(256, parameters={"kid": identificador})
    else:
        raise ValueError(
            f"Algoritmo no soportado: {algorithm!r}. Soportados: Ed25519 (recomendado), "
            f"Ed448, EdDSA (deprecado por RFC 9864), RS*/PS*, HS*."
        )

    privada = json.dumps(par.as_dict(private=True))
    # Para HS* la clave es simétrica: no hay "pública". Se guarda la misma, y el almacén
    # simétrico tiene que estar aislado del asimétrico — ver la nota de `jwks_document`.
    publica = (
        privada if algorithm.startswith("HS") else json.dumps(par.as_dict(private=False))
    )

    return SigningKey(
        kid=identificador,
        algorithm=algorithm,
        public_key=publica,
        private_key=privada,
        status="active",
    )


class AbstractKeyStore(abc.ABC):
    """
    De dónde salen las claves de firma.

    `get` recibe el `kid` **del token**, o sea un valor controlado por quien lo presenta. La
    implementación tiene que tratarlo como entrada hostil: no interpolarlo en una consulta, no
    usarlo como ruta de archivo, y no dejar que un flood de `kid` inventados se convierta en un
    flood de consultas (ver la caché negativa de `tokens.py`).
    """

    @abc.abstractmethod
    async def get(self, kid: str) -> SigningKey | None:
        """La clave con ese `kid`, o `None` si no existe. **No lanza por un kid desconocido.**"""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_active(self) -> SigningKey:
        """
        La clave con la que firmar.

        Raises:
            NoActiveKeyError: si no hay ninguna `active`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def list_verifiable(self) -> list[SigningKey]:
        """Las que pueden verificar (`active` + `verify_only`). Es lo que publica el JWKS."""
        raise NotImplementedError


class StaticKeyStore(AbstractKeyStore):
    """
    Almacén en memoria. Para tests, desarrollo, y despliegues de un solo proceso.

    Thread-safe con `RLock`: `rotate()` muta el estado de varias claves y una lectura
    concurrente vería un estado intermedio donde hay dos `active` o ninguna.

    Uso::

        almacen = StaticKeyStore([generate_signing_key()])

        nueva = await almacen.rotate()          # la vieja pasa a verify_only
        await almacen.retire(clave_vieja.kid)   # recién después del TTL máximo de token
    """

    def __init__(self, keys: t.Iterable[SigningKey] | None = None) -> None:
        self._keys: dict[str, SigningKey] = {}
        self._lock = threading.RLock()
        for clave in keys or ():
            self._keys[clave.kid] = clave

    async def get(self, kid: str) -> SigningKey | None:
        with self._lock:
            return self._keys.get(kid)

    async def get_active(self) -> SigningKey:
        with self._lock:
            for clave in self._keys.values():
                if clave.can_sign:
                    return clave
        raise NoActiveKeyError(
            "No hay ninguna clave con estado 'active' para firmar. Si acabás de rotar, "
            "verificá que la nueva quedó activa; si el almacén está vacío, sembralo con "
            "`generate_signing_key()`."
        )

    async def list_verifiable(self) -> list[SigningKey]:
        with self._lock:
            return [clave for clave in self._keys.values() if clave.can_verify]

    # ── Rotación ──────────────────────────────────────────────────────────────
    async def rotate(self, *, algorithm: str | None = None) -> SigningKey:
        """
        Genera una clave nueva y pasa la activa a `verify_only`.

        **No retira nada.** Retirar en el mismo paso invalidaría todo token en vuelo, que es
        exactamente lo que el estado intermedio evita. `retire()` se llama después, cuando
        pasó el TTL máximo de token.
        """
        with self._lock:
            anterior = next((k for k in self._keys.values() if k.can_sign), None)
            nueva = generate_signing_key(
                algorithm=algorithm or (anterior.algorithm if anterior else DEFAULT_ALGORITHM)
            )
            if anterior is not None:
                anterior.status = "verify_only"
            self._keys[nueva.kid] = nueva
            return nueva

    async def retire(self, kid: str) -> None:
        """
        Retira una clave: deja de verificar.

        Sólo después de que venció el último token que pudo haber firmado. Antes de eso, cada
        token en vuelo con ese `kid` se convierte en un 401.
        """
        with self._lock:
            clave = self._keys.get(kid)
            if clave is None:
                raise UnknownKeyError(f"No existe ninguna clave con kid {kid!r}.")
            clave.status = "retired"

    def add(self, key: SigningKey) -> None:
        with self._lock:
            self._keys[key.kid] = key


async def jwks_document(store: AbstractKeyStore) -> dict[str, t.Any]:
    """
    El documento para `/.well-known/jwks.json`.

    Publica **sólo las públicas de las claves verificables**: las `retired` quedan afuera —
    publicarlas invitaría a un verificador externo a aceptar tokens que nosotros ya
    rechazamos.

    ⚠️ **Las claves simétricas (`HS*`) no se publican nunca.** En una clave simétrica el
    "material público" *es* el secreto de firma, así que publicarlo le permite a cualquiera
    emitir tokens válidos. Se filtran acá explícitamente en vez de confiar en que nadie
    configure `HS256`; y por el mismo motivo el almacén simétrico y el asimétrico tienen que
    estar separados, que es lo que evita la confusión de algoritmo
    HS256-firmado-con-la-clave-pública.
    """
    import json

    claves: list[dict[str, t.Any]] = []
    for clave in await store.list_verifiable():
        if clave.algorithm.startswith("HS"):
            continue
        publica = json.loads(clave.public_key)
        publica.setdefault("kid", clave.kid)
        publica["alg"] = clave.algorithm
        publica["use"] = "sig"
        # Defensa en profundidad: si el material privado se colara en `public_key` por un bug
        # de serialización, no sale por acá.
        for privado in ("d", "p", "q", "dp", "dq", "qi", "k"):
            publica.pop(privado, None)
        claves.append(publica)

    return {"keys": claves}
