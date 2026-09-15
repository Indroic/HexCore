import type { CookieJar } from "./cookie-jar";
import { documentCookieJar } from "./cookie-jar";
import type { DarwinTokens, Transport } from "./types";

/** Métodos que el double-submit CSRF no protege: no mutan estado, así que no lo necesitan. */
const METODOS_SEGUROS = new Set(["GET", "HEAD", "OPTIONS"]);

export interface CookieTransportOptions {
  /**
   * El nombre de la cookie CSRF a leer. Tiene que coincidir con lo que el despliegue
   * configuró en `CookieConfig.name_for("csrf")` del lado del servidor — si no coinciden,
   * cada request no-seguro recibe un 403 de `CsrfValidationError`.
   */
  csrfCookieName?: string;

  /**
   * De dónde leer la cookie CSRF. Por defecto, `documentCookieJar()` — sólo funciona en un
   * navegador de verdad. **Fuera del navegador, la recomendación es usar `transport:
   * "bearer"`**: este jar existe para SSR (reenviar la cookie del request entrante) y para
   * tests, no como modo de producción.
   */
  cookieJar?: CookieJar;
}

/**
 * El transporte de cookie: `HttpOnly` para el access y el refresh, con CSRF double-submit en
 * los métodos que mutan.
 *
 * `persist` y `clear` son no-ops a propósito: el navegador ya escribió (o borró) las cookies
 * vía `Set-Cookie`/`Set-Cookie` con `Max-Age=0` — no hay ningún estado del lado del cliente
 * que este transporte tenga que administrar.
 */
export class CookieTransport implements Transport {
  readonly name = "cookie" as const;

  private readonly csrfCookieName: string;
  private readonly jar: CookieJar;

  constructor(options: CookieTransportOptions = {}) {
    this.csrfCookieName = options.csrfCookieName ?? "darwin_csrf";

    if (options.cookieJar) {
      this.jar = options.cookieJar;
    } else if (typeof document !== "undefined") {
      this.jar = documentCookieJar();
    } else {
      throw new Error(
        "CookieTransport se construyó sin `cookieJar` fuera de un navegador. Pasá uno " +
          "explícito (memoryCookieJar() para SSR) o, si es posible, usá `transport: " +
          '"bearer"` en este contexto — es la opción recomendada fuera del navegador.',
      );
    }
  }

  async requestHeaders(method: string): Promise<Record<string, string>> {
    const headers: Record<string, string> = { "X-Darwin-Transport": "cookie" };

    if (METODOS_SEGUROS.has(method.toUpperCase())) {
      return headers;
    }

    // Si la cookie CSRF no está, no se falla localmente: el servidor sólo la exige cuando
    // vino la cookie de sesión, y el 401/403 que corresponde en ese caso es más informativo
    // que un error inventado acá. Ver el docstring de `CsrfValidationError` del lado Python.
    const csrf = await this.jar.get(this.csrfCookieName);
    if (csrf) {
      headers["X-CSRF-Token"] = csrf;
    }

    return headers;
  }

  persist(_tokens: DarwinTokens): void {
    // No-op: ver el docstring de la clase.
  }

  clear(): void {
    // No-op: ver el docstring de la clase.
  }
}
