"""
Implementaciones de `AbstractClock`.

El reloj es un puerto para que los tests de TTL, ventanas de rotación y vencimiento de
impersonación no necesiten `freezegun` ni `time-machine`: se inyecta `FixedClock` y se lo
adelanta a mano. Es la razón por la que Darwin **no agrega ninguna dependencia de desarrollo**
al proyecto.

`FixedClock` vive acá y no en el kit de testing a propósito: lo necesita cualquiera que quiera
testear código que dependa del tiempo, incluida una app del consumidor, y esconderlo en
`hexcore.darwin.testing` lo volvería inalcanzable desde la fachada.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from hexcore.darwin.domain.ports import AbstractClock

__all__ = ["SystemClock", "FixedClock"]


class SystemClock(AbstractClock):
    """
    El reloj real. **Siempre tz-aware en UTC.**

    Devolver naive sería el error caro: comparar un naive con el `expires_at` aware que sale
    de la base lanza ``TypeError: can't compare offset-naive and offset-aware datetimes``, y
    lo hace en el camino de verificación de sesión — o sea en cada petición autenticada.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock(AbstractClock):
    """
    Reloj controlado a mano, para tests.

    Thread-safe con `RLock` porque los tests de concurrencia lo comparten entre corutinas y
    entre hilos, y un `advance()` mientras otro lee daría un valor a medio escribir.

    Uso::

        reloj = FixedClock(datetime(2026, 8, 6, tzinfo=UTC))
        verificador = JoserfcTokenVerifier(..., clock=reloj)

        reloj.advance(seconds=121)          # el access token venció
        with pytest.raises(TokenExpiredError):
            verificador.verify(token, transport="cookie")
    """

    def __init__(self, moment: datetime | None = None) -> None:
        inicial = moment if moment is not None else datetime.now(UTC)
        if inicial.tzinfo is None:
            raise ValueError(
                "FixedClock necesita un datetime tz-aware. Un reloj naive esconde "
                "exactamente el bug que este puerto existe para poder testear: "
                "pasá `tzinfo=UTC`."
            )
        self._momento = inicial
        self._lock = threading.RLock()

    def now(self) -> datetime:
        with self._lock:
            return self._momento

    def advance(self, *, seconds: float = 0, minutes: float = 0, days: float = 0) -> datetime:
        """Adelanta el reloj y devuelve el instante nuevo."""
        with self._lock:
            self._momento += timedelta(seconds=seconds, minutes=minutes, days=days)
            return self._momento

    def set(self, moment: datetime) -> None:
        """Fija el reloj en un instante concreto."""
        if moment.tzinfo is None:
            raise ValueError("FixedClock.set necesita un datetime tz-aware.")
        with self._lock:
            self._momento = moment
