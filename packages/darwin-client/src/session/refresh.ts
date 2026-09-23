import { isDefinitiveAuthFailure } from "../core/errors";
import type { SessionResponse } from "../core/types";

export interface RefreshControllerOptions {
  /**
   * El `POST /auth/refresh` crudo. Tiene que venir con `skipProactiveRefresh` y
   * `skipReactiveRetry` en `true` del lado de quien lo arma — un refresh que se refresca a sí
   * mismo, o que reintenta contra su propio 401, es la recursión que este módulo existe para
   * evitar.
   */
  doRefresh: () => Promise<SessionResponse>;
  onSuccess: (session: SessionResponse) => void | Promise<void>;
  onFailure: (error: unknown) => void;
}

export interface RefreshController {
  /**
   * Refresca. **Single-flight**: si ya hay un refresh en curso, todos los llamadores esperan
   * *esa* misma promesa en vez de disparar uno cada uno.
   *
   * Es el bug más caro que este cliente existe para evitar: sin esto, el refresh **rota** el
   * token y la detección de reuso del servidor mata la familia entera — diez refreshes en
   * paralelo con el access vencido es un auto-logout garantizado, no una carrera inofensiva.
   *
   * Si un refresh anterior ya falló **con un veredicto real del servidor**
   * (`isDefinitiveAuthFailure`) — el refresh token es inválido, fue revocado, o la sesión
   * llegó a su techo — esta llamada rechaza **sin pegarle a la red**: la sesión quedó marcada
   * muerta (`markAlive()` la limpia en el próximo `signIn` exitoso). Un fallo transitorio
   * (`NetworkError`, `NonJsonResponse` — no hubo veredicto, sólo un problema de transporte)
   * **no** marca la sesión muerta: no hay motivo para forzar un re-login por un blip de red
   * cuando el refresh token en sí puede seguir siendo válido.
   */
  refresh: () => Promise<void>;
  /** Limpia la marca de muerta. Se llama tras un `signIn`/`signOut` exitoso. */
  markAlive: () => void;
}

export function createRefreshController(
  options: RefreshControllerOptions,
): RefreshController {
  // `null` en un `finally` es la parte que importa: sin eso, el próximo refresh después de que
  // éste termine devolvería una promesa ya resuelta con tokens viejos en vez de disparar uno
  // nuevo.
  let enVuelo: Promise<void> | null = null;
  let muerta = false;

  function refresh(): Promise<void> {
    if (muerta) {
      return Promise.reject(
        new Error(
          "The refresh already failed once and the session was marked dead. It is not " +
            "retried against the server without a signIn/markAlive() in between.",
        ),
      );
    }

    if (enVuelo) return enVuelo;

    enVuelo = ejecutar().finally(() => {
      enVuelo = null;
    });

    return enVuelo;
  }

  async function ejecutar(): Promise<void> {
    try {
      const session = await options.doRefresh();
      await options.onSuccess(session);
    } catch (error) {
      if (isDefinitiveAuthFailure(error)) muerta = true;
      options.onFailure(error);
      throw error;
    }
  }

  function markAlive(): void {
    muerta = false;
  }

  return { refresh, markAlive };
}
