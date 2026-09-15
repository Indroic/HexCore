/**
 * De dónde `CookieTransport` lee la cookie CSRF (double-submit).
 *
 * `get` puede ser async porque la `CookieStore` API del navegador (`cookieStoreJar`) es
 * async por naturaleza — no hay forma de leerla sync sin `document.cookie`.
 */
export interface CookieJar {
  get(name: string): string | undefined | Promise<string | undefined>;
}

/**
 * Lee de `document.cookie`. El default en el navegador.
 *
 * No cachea: `document.cookie` cambia por debajo (el servidor la re-emite en cada request,
 * con `max_age` igual al del access token — 120 s por default), así que cachear el valor
 * mandaría un CSRF token vencido después de un refresh.
 */
export function documentCookieJar(): CookieJar {
  return {
    get(name) {
      if (typeof document === "undefined") return undefined;
      const escapado = name.replace(/[.$?*|{}()[\]\\/+^]/g, "\\$&");
      const match = document.cookie.match(new RegExp(`(?:^|; )${escapado}=([^;]*)`));
      const valor = match?.[1];
      return valor !== undefined ? decodeURIComponent(valor) : undefined;
    },
  };
}

/**
 * Un jar en memoria, poblado a mano desde un header `Cookie` — para SSR (reenviar la cookie
 * del request entrante) y para tests. **No** es un modo de producción fuera del navegador: la
 * recomendación documentada ahí es `transport: "bearer"`. Ver el docstring de `CookieTransport`.
 */
export function memoryCookieJar(cookieHeader = ""): CookieJar {
  const cookies = new Map<string, string>();
  for (const parte of cookieHeader.split(";")) {
    const separador = parte.indexOf("=");
    if (separador === -1) continue;
    const nombre = parte.slice(0, separador).trim();
    if (!nombre) continue;
    cookies.set(nombre, decodeURIComponent(parte.slice(separador + 1).trim()));
  }

  return {
    get(name) {
      return cookies.get(name);
    },
  };
}

/**
 * La `CookieStore` API (`window.cookieStore`), donde está disponible. Async por naturaleza:
 * a diferencia de `document.cookie`, no hace un parse de string en el hilo principal.
 */
export function cookieStoreJar(): CookieJar {
  return {
    async get(name) {
      const store = (
        globalThis as {
          cookieStore?: { get(name: string): Promise<{ value: string } | null> };
        }
      ).cookieStore;
      if (!store) {
        throw new Error(
          "cookieStoreJar() necesita `window.cookieStore` (CookieStore API), que este " +
            "entorno no expone. Usá documentCookieJar() o memoryCookieJar() en su lugar.",
        );
      }
      const cookie = await store.get(name);
      return cookie?.value;
    },
  };
}
