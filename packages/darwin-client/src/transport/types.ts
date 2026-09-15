/** Los dos transportes que Darwin acepta. Ver `hexcore.darwin.infrastructure.transports`. */
export type DarwinTransportName = "cookie" | "bearer";

/**
 * El par de tokens tal como lo emite el servidor (`SessionResponse`).
 *
 * `accessToken`/`refreshToken` son opcionales porque el camino de cookie no los trae: van en
 * `Set-Cookie`, y el servidor no los repite en el cuerpo — devolverlos además daría una copia
 * que puede terminar en `localStorage`, que es justamente lo que `HttpOnly` evita.
 */
export interface DarwinTokens {
  sessionId: string;
  expiresIn: number;
  tokenType: string;
  accessToken?: string | undefined;
  refreshToken?: string | undefined;
}

/**
 * Cómo un transporte participa en un request: qué headers agrega, y qué hace con los tokens
 * que la respuesta trae.
 *
 * El cliente manda `X-Darwin-Transport` en **absolutamente todos** los requests, no sólo
 * cuando hay ambigüedad — es lo que hace `requestHeaders` en las dos implementaciones. El
 * backend cae al default del despliegue cuando ese header no viene, así que declararlo
 * siempre elimina una clase entera de bugs dependientes de la configuración del servidor.
 */
export interface Transport {
  readonly name: DarwinTransportName;

  /** Los headers a agregar a un request con este método HTTP. */
  requestHeaders(method: string): Record<string, string> | Promise<Record<string, string>>;

  /**
   * Guarda los tokens recién emitidos (sign-in, refresh, 2FA challenge, OAuth callback...).
   * En cookie es un no-op: el navegador ya los persistió vía `Set-Cookie`.
   */
  persist(tokens: DarwinTokens): void | Promise<void>;

  /** Limpia cualquier estado local (sign-out). En cookie también es un no-op. */
  clear(): void | Promise<void>;
}
