import type { CookieJar } from "./cookie-jar";
import { documentCookieJar } from "./cookie-jar";
import type { DarwinTokens, Transport } from "./types";

/** Métodos que el double-submit CSRF no protege: no mutan estado, así que no lo necesitan. */
const METODOS_SEGUROS = new Set(["GET", "HEAD", "OPTIONS"]);

/**
 * El default de `CookieConfig.csrf_name` del lado Python, **sin** el prefijo.
 *
 * Que sea el nombre base y no el nombre final es deliberado: cuál de los dos emite el servidor
 * depende de `CookieConfig.secure`, y eso cambia entre desarrollo y producción del mismo
 * despliegue. Ver `nombresCandidatos`.
 */
const CSRF_POR_DEFECTO = "csrf";

/**
 * El prefijo que el servidor le agrega a sus cookies con `secure=True`. Lo exige el navegador:
 * una cookie `__Host-` sólo se acepta por HTTPS, con `Path=/` y sin `Domain`.
 */
const PREFIJO_HOST = "__Host-";

/**
 * Los nombres bajo los que buscar la cookie CSRF, en orden de preferencia.
 *
 * El servidor no emite un nombre fijo: `CookieConfig.name_for("csrf")` devuelve `__Host-csrf`
 * con `secure=True` —el default, o sea producción— y `csrf` pelado con `secure=False`, que es
 * como se corre sobre HTTP en desarrollo. Un cliente con un único nombre hardcodeado acierta en
 * un entorno y falla en el otro, y falla **en silencio**: sin la cookie no se manda el header,
 * y el servidor responde 403 `CsrfValidationError` en cada método que muta mientras los `GET`
 * siguen andando. Eso se lee como un bug de permisos y no lo es.
 *
 * Por eso se prueban los dos. Si quien llama ya pasó un nombre con el prefijo puesto, se
 * respeta tal cual: agregárselo de nuevo daría `__Host-__Host-csrf`.
 */
function nombresCandidatos(base: string): readonly string[] {
  if (base.startsWith(PREFIJO_HOST)) return [base];
  return [PREFIJO_HOST + base, base];
}

export interface CookieTransportOptions {
  /**
   * El nombre **base** de la cookie CSRF, sin el prefijo `__Host-`. Tiene que coincidir con el
   * `CookieConfig.csrf_name` del despliegue; por defecto `"csrf"`, que es el default del lado
   * Python.
   *
   * No hace falta ajustarlo entre desarrollo y producción: el transporte busca
   * `__Host-<nombre>` y `<nombre>`, que son los dos nombres que
   * `CookieConfig.name_for("csrf")` puede devolver según `secure`. Pasar el nombre ya
   * prefijado también funciona, y entonces se usa exactamente ése.
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

  private readonly csrfCookieNames: readonly string[];
  private readonly jar: CookieJar;

  constructor(options: CookieTransportOptions = {}) {
    this.csrfCookieNames = nombresCandidatos(options.csrfCookieName ?? CSRF_POR_DEFECTO);

    if (options.cookieJar) {
      this.jar = options.cookieJar;
    } else if (typeof document !== "undefined") {
      this.jar = documentCookieJar();
    } else {
      throw new Error(
        "CookieTransport was constructed without `cookieJar` outside a browser. Pass one " +
          "explicitly (memoryCookieJar() for SSR) or, if you can, use `transport: " +
          '"bearer"` in this context — it is the recommended option outside the browser.',
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
    for (const nombre of this.csrfCookieNames) {
      const csrf = await this.jar.get(nombre);
      if (csrf) {
        headers["X-CSRF-Token"] = csrf;
        break;
      }
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
