"""
El sobre firmado que lleva el `AuthContext` a través de una cola.

Es la implementación Darwin del punto de extensión de
`hexcore.domain.cqrs.envelope`: un proveedor que sella el contexto ambiental al encolar, y
un restaurador que lo verifica y lo republica en el worker.

Formato del valor sellado::

    <b64url(json)>.<b64url(hmac_sha256)>

Compacto, sin padding, y sin depender de joserfc: esto no es un JWT y no debe parecerlo. Un
JWT invita a que alguien lo presente como credencial en un endpoint, y este valor **no es
una credencial de portador** —no lo emite un login, no lo ve un cliente, y vale sólo
adjuntado al mensaje al que se ató—. Usar un formato distinto es lo que evita que los dos
caminos se confundan.

Se firma con `IdentityConfig.secret_key` (simétrico) y **no** con la clave del JWKS: el
sobre lo produce y lo consume el mismo despliegue, así que no hace falta verificación por
terceros ni rotación pública, y una firma HMAC es un orden de magnitud más barata en un
camino que corre por cada mensaje encolado. El input del MAC va prefijado con una etiqueta
de dominio (`_ETIQUETA`), así que el mismo secreto usado en otro protocolo no puede producir
una falsificación cruzada.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import typing as t
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import UUID

from hexcore.darwin.domain.context import (
    AuthContext,
    Impersonation,
    Principal,
    SystemPrincipal,
    auth_scope,
)
from hexcore.darwin.domain.exceptions import WorkerContextIntegrityError
from hexcore.domain.cqrs.envelope import (
    AbstractEnvelopeRestorer,
    message_correlation_id,
)
from hexcore.domain.cqrs.resolution import build_fqn

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.ports import AbstractClock, AbstractSessionRepository

__all__ = [
    "ENVELOPE_KEY",
    "ENVELOPE_VERSION",
    "AUTH_RESTORER",
    "AuthEnvelopeCodec",
    "AuthEnvelopeRestorer",
    "auth_envelope_provider",
]

#: La clave del sobre bajo la que viaja el contexto de Darwin.
ENVELOPE_KEY = "auth"

#: Versión del formato sellado. Se verifica y un valor desconocido se rechaza.
#:
#: Con versión desde el día uno porque el sobre tiene TTL: cuando el formato cambie va a
#: haber mensajes de los dos formatos en la cola al mismo tiempo, y sin este campo el
#: síntoma sería un `WorkerContextIntegrityError` sin causa aparente durante la ventana del
#: deploy.
ENVELOPE_VERSION = 1

#: Separación de dominio del MAC. Sin esto, el mismo secreto firmando otro protocolo
#: permitiría reusar una firma de allá como sobre válido acá.
_ETIQUETA = b"hexcore.darwin.envelope.v1"

#: Tolerancia para un sobre fechado en el futuro. Existe por desfasaje de reloj entre el
#: proceso que encola y el que consume, que en un cluster es normal y no es un ataque.
_TOLERANCIA_RELOJ = timedelta(seconds=60)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(texto: str) -> bytes:
    # El padding se repone: `urlsafe_b64decode` lo exige y el formato no lo lleva.
    relleno = "=" * (-len(texto) % 4)
    return base64.urlsafe_b64decode(texto + relleno)


def _epoch(momento: datetime) -> int:
    return int(momento.timestamp())


def _serializar_principal(principal: Principal | SystemPrincipal) -> dict[str, t.Any]:
    """
    Aplana un principal a JSON, distinguiendo la clase con `kind`.

    `kind` explícito y no inferido de qué campos hay: un `SystemPrincipal` sin scopes y un
    `Principal` sin sesión ni email tienen la misma forma, y adivinar entre los dos daría un
    actor de sistema donde había un usuario —o al revés, que es peor, porque un
    `SystemPrincipal` no responde a la denylist ni a la revocación por generación.
    """
    if isinstance(principal, SystemPrincipal):
        return {
            "kind": "system",
            "name": principal.name,
            "scopes": sorted(principal.scopes),
        }
    return {
        "kind": "user",
        "id": str(principal.user_id),
        "sid": str(principal.session_id) if principal.session_id else None,
        "email": principal.email,
        "roles": sorted(principal.roles),
        "scopes": sorted(principal.scopes),
    }


def _deserializar_principal(raw: t.Any) -> Principal | SystemPrincipal:
    if not isinstance(raw, dict):
        raise WorkerContextIntegrityError("El sobre trae un principal que no es un objeto.")
    datos = t.cast(dict[str, t.Any], raw)
    if datos.get("kind") == "system":
        return SystemPrincipal(
            name=str(datos["name"]), scopes=frozenset(datos.get("scopes") or ())
        )
    return Principal(
        user_id=UUID(str(datos["id"])),
        session_id=UUID(str(datos["sid"])) if datos.get("sid") else None,
        email=datos.get("email"),
        roles=frozenset(datos.get("roles") or ()),
        scopes=frozenset(datos.get("scopes") or ()),
    )


class AuthEnvelopeCodec:
    """
    Sella y abre el sobre de autenticación.

    Uso::

        codec = AuthEnvelopeCodec(secret="…", clock=SystemClock(), ttl=timedelta(hours=24))
        sellado = codec.seal(contexto, comando)
        contexto_restaurado = codec.open(sellado, comando)
    """

    def __init__(
        self,
        *,
        secret: str,
        clock: "AbstractClock",
        ttl: timedelta = timedelta(hours=24),
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._clock = clock
        self._ttl = ttl

    # ── Sellar ────────────────────────────────────────────────────────────────
    def seal(self, context: "AuthContext[t.Any]", message: t.Any) -> str:
        """
        Sella `context` atado a `message`.

        Los dos campos de atadura son el punto del diseño:

        - `mt`: el FQN del **tipo** del mensaje.
        - `cid`: el `command_id` / `event_id` del mensaje.

        Sin ellos, capturar el sobre de un `DeleteAccount` legítimo y re-adjuntarlo a un
        `TransferFunds` da un sobre que verifica —está bien firmado— y el worker ejecuta la
        transferencia con la autoridad del grant de borrado.

        `context.user` **no viaja**: es el modelo extendido de la aplicación, de tipo
        arbitrario y sin garantía de ser serializable. El contexto restaurado lo tiene en
        `None`, y un handler de background que lo necesite lo carga del repositorio con
        `subject_id`. Serializarlo "cuando se pueda" daría un campo que existe o no según el
        tipo, que es la clase de contrato que nadie puede programar en contra.
        """
        ahora = self._clock.now()
        cuerpo: dict[str, t.Any] = {
            "v": ENVELOPE_VERSION,
            "actor": _serializar_principal(context.actor),
            "subject": _serializar_principal(context.subject),
            "cid": message_correlation_id(message),
            "mt": build_fqn(type(message)),
            "iat": _epoch(ahora),
            "exp": _epoch(ahora + self._ttl),
        }
        if context.impersonation is not None:
            cuerpo["imp"] = {
                "granted_by": str(context.impersonation.granted_by),
                "reason": context.impersonation.reason,
                "granted_at": _epoch(context.impersonation.granted_at),
                "expires_at": _epoch(context.impersonation.expires_at),
            }

        # `sort_keys` y separadores fijos: el valor firmado tiene que ser reproducible byte a
        # byte, porque el MAC se calcula sobre el texto y no sobre la estructura.
        crudo = json.dumps(cuerpo, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload = _b64(crudo)
        return f"{payload}.{self._firmar(payload)}"

    # ── Abrir ─────────────────────────────────────────────────────────────────
    def open(self, sealed: t.Any, message: t.Any) -> "AuthContext[t.Any]":
        """
        Verifica el sobre y reconstruye el contexto.

        El orden de los chequeos no es casual: **primero el MAC**, después el contenido.
        Parsear el JSON antes de verificar la firma sería procesar entrada controlada por
        quien escriba en el broker, y cada chequeo que corre antes del MAC es una diferencia
        de tiempo o de error que informa al atacante.

        El `transport` del contexto restaurado es siempre ``"worker"``, nunca el original.
        Un job de background no está sirviendo un request con cookie, y código que ramifica
        por transporte —el chequeo anti-CSRF, por ejemplo— tiene que poder distinguirlo. El
        transporte original queda en el registro de auditoría del request que encoló, que es
        donde se lo va a buscar.

        Raises:
            WorkerContextIntegrityError: si el formato, la firma, la atadura al mensaje o la
                ventana temporal no verifican.
        """
        if not isinstance(sealed, str) or sealed.count(".") != 1:
            raise WorkerContextIntegrityError(
                "El sobre de autenticación no tiene la forma '<payload>.<firma>'."
            )

        payload, firma = sealed.split(".", 1)
        if not hmac.compare_digest(self._firmar(payload), firma):
            raise WorkerContextIntegrityError(
                "La firma del sobre de autenticación no verifica. O el secreto de firma no "
                "es el mismo en el proceso que encoló y en el que consume, o alguien está "
                "escribiendo en el broker."
            )

        try:
            cuerpo = t.cast(dict[str, t.Any], json.loads(_unb64(payload)))
        except Exception as exc:  # noqa: BLE001 — cualquier fallo acá es integridad
            raise WorkerContextIntegrityError(
                f"El sobre está firmado pero su contenido no es JSON válido: {exc}"
            ) from exc

        if cuerpo.get("v") != ENVELOPE_VERSION:
            raise WorkerContextIntegrityError(
                f"Versión de sobre desconocida: {cuerpo.get('v')!r}. Este proceso entiende "
                f"la {ENVELOPE_VERSION}; probablemente falte terminar un deploy."
            )

        self._verificar_atadura(cuerpo, message)
        self._verificar_ventana(cuerpo)

        try:
            return AuthContext(
                actor=_deserializar_principal(cuerpo.get("actor")),
                subject=_deserializar_principal(cuerpo.get("subject")),
                transport="worker",
                impersonation=self._impersonacion(cuerpo.get("imp")),
            )
        except WorkerContextIntegrityError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Incluye el invariante de `AuthContext`: un sobre cuyo subject difiere del actor
            # sin permiso de impersonación no puede reconstruirse, y eso es correcto —el
            # invariante vale igual del otro lado de la cola.
            raise WorkerContextIntegrityError(
                f"El sobre verifica pero no describe un contexto válido: {exc}"
            ) from exc

    # ── Internos ──────────────────────────────────────────────────────────────
    def _firmar(self, payload: str) -> str:
        mac = hmac.new(
            self._secret, _ETIQUETA + b"." + payload.encode("ascii"), hashlib.sha256
        )
        return _b64(mac.digest())

    def _verificar_atadura(self, cuerpo: dict[str, t.Any], message: t.Any) -> None:
        esperado_tipo = build_fqn(type(message))
        if cuerpo.get("mt") != esperado_tipo:
            raise WorkerContextIntegrityError(
                f"El sobre venía atado a '{cuerpo.get('mt')}' y llegó adjunto a "
                f"'{esperado_tipo}'. Un grant re-adjuntado a otro mensaje se rechaza."
            )

        esperado_id = message_correlation_id(message)
        if cuerpo.get("cid") != esperado_id:
            raise WorkerContextIntegrityError(
                f"El sobre venía atado al mensaje {cuerpo.get('cid')!r} y llegó adjunto al "
                f"{esperado_id!r}."
            )

    def _verificar_ventana(self, cuerpo: dict[str, t.Any]) -> None:
        ahora = self._clock.now()
        exp = cuerpo.get("exp")
        iat = cuerpo.get("iat")
        if not isinstance(exp, int) or not isinstance(iat, int):
            raise WorkerContextIntegrityError("El sobre no declara 'iat' y 'exp' enteros.")

        if _epoch(ahora) >= exp:
            raise WorkerContextIntegrityError(
                "El sobre de autenticación venció. Un payload rescatado de una dead-letter "
                "queue no se puede reproducir indefinidamente."
            )

        if iat > _epoch(ahora + _TOLERANCIA_RELOJ):
            raise WorkerContextIntegrityError(
                "El sobre está fechado en el futuro más allá de la tolerancia de reloj."
            )

    def _impersonacion(self, raw: t.Any) -> Impersonation | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise WorkerContextIntegrityError(
                "El sobre trae una impersonación que no es un objeto."
            )
        datos = t.cast(dict[str, t.Any], raw)
        from datetime import UTC

        return Impersonation(
            granted_by=UUID(str(datos["granted_by"])),
            reason=str(datos["reason"]),
            granted_at=datetime.fromtimestamp(int(datos["granted_at"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(datos["expires_at"]), tz=UTC),
        )


# ── El proveedor ──────────────────────────────────────────────────────────────
def auth_envelope_provider(message: t.Any) -> str | None:
    """
    Proveedor del sobre: sella el contexto ambiental, o `None` si no hay ninguno.

    `None` y no un error cuando no hay contexto: encolar sin estar autenticado es legítimo
    —un cron, un seed, la CLI— y exigir contexto rompería todo el uso de background que no
    tiene nada que ver con identidad.

    Resuelve el contenedor en cada llamada en vez de capturarlo al registrarse: el registro
    ocurre en `configure_identity()`, que es perezoso por diseño, y capturar el códec ahí
    forzaría a construirlo —y por lo tanto a exigir la clave de firma— en import time.
    """
    from hexcore.darwin.domain.context import current_auth

    contexto = current_auth()
    if contexto is None:
        return None

    from hexcore.darwin.application.container import get_identity_container

    return get_identity_container().envelope_codec().seal(contexto, message)


# ── El restaurador ────────────────────────────────────────────────────────────
class AuthEnvelopeRestorer(AbstractEnvelopeRestorer):
    """
    Verifica el sobre, revalida la sesión contra el almacén y republica el contexto.

    Los tres pasos, y el segundo es el que no se puede saltear: verificar la firma y el `exp`
    sólo prueba que el sobre es auténtico y reciente, no que la sesión siga viva. Entre el
    encolado y la ejecución el usuario pudo cerrar sesión, el admin pudo revocarla, o la
    detección de reuso de refresh pudo tirar la familia entera. Un TTL de 24 h sin este
    chequeo son 24 h de ejecución con una credencial revocada.

    Sólo se revalida cuando el actor **tiene** `session_id`. Un `SystemPrincipal` no tiene
    sesión revocable —su autoridad es el cableado del proceso— y un `Principal` sin sesión
    sólo puede haber salido de `system_context()` o de una construcción explícita en
    proceso, ninguna de las cuales tiene fila que consultar.
    """

    def __init__(
        self,
        *,
        codec: AuthEnvelopeCodec,
        sessions: "AbstractSessionRepository",
        clock: "AbstractClock",
    ) -> None:
        self._codec = codec
        self._sessions = sessions
        self._clock = clock

    @asynccontextmanager
    async def restore(self, value: t.Any, message: t.Any) -> t.AsyncIterator[None]:
        contexto = self._codec.open(value, message)
        await self._revalidar(contexto)
        with auth_scope(contexto):
            yield

    async def _revalidar(self, contexto: "AuthContext[t.Any]") -> None:
        actor = contexto.actor
        if not isinstance(actor, Principal) or actor.session_id is None:
            return

        sesion = await self._sessions.get(actor.session_id)
        if sesion is None:
            raise WorkerContextIntegrityError(
                f"El sobre nombra la sesión {actor.session_id} y esa fila no existe."
            )

        # `consumed_at` cuenta como no viva, y en este camino es lo esperable y no un error:
        # toda rotación de refresh consume la fila anterior, así que un mensaje encolado
        # antes de un refresh trae un `sid` ya consumido. Se rechaza igual —esa sesión ya no
        # es la vigente— y quien necesite sobrevivir a una rotación tiene que llevar el
        # dato que le haga falta en el propio comando.
        if not sesion.is_live_at(self._clock.now()):
            raise WorkerContextIntegrityError(
                f"La sesión {actor.session_id} ya no está viva: fue revocada, rotada o "
                f"venció entre el encolado y la ejecución."
            )


class _ContainerAuthRestorer(AbstractEnvelopeRestorer):
    """
    Restaurador que delega en el del contenedor, resolviéndolo en cada uso.

    Existe por la misma razón que `auth_envelope_provider` resuelve perezosamente: lo que se
    registra en el núcleo tiene que poder registrarse en `configure_identity()`, que es
    perezoso por diseño. Registrar el restaurador concreto forzaría a construir el códec —y
    con él a exigir la clave de firma— y a instanciar el repositorio de sesiones, que
    necesita el extra `[darwin]`, en el momento del cableado.
    """

    def restore(self, value: t.Any, message: t.Any) -> t.AsyncContextManager[None]:
        from hexcore.darwin.application.container import get_identity_container

        return get_identity_container().envelope_restorer().restore(value, message)


#: El restaurador que `configure_identity()` registra en el núcleo. Singleton porque no tiene
#: estado: todo lo resuelve del contenedor en el momento de usarse.
AUTH_RESTORER: AbstractEnvelopeRestorer = _ContainerAuthRestorer()
