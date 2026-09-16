import { describe, expect, it } from "vitest";
import { CookieTransport } from "../../src/transport/cookie";
import { memoryCookieJar } from "../../src/transport/cookie-jar";

describe("CookieTransport", () => {
  it("manda X-Darwin-Transport en absolutamente todos los requests", async () => {
    const transporte = new CookieTransport({ cookieJar: memoryCookieJar() });

    for (const metodo of ["GET", "POST", "DELETE", "PATCH"]) {
      const headers = await transporte.requestHeaders(metodo);
      expect(headers["X-Darwin-Transport"]).toBe("cookie");
    }
  });

  it("no agrega X-CSRF-Token en métodos seguros aunque la cookie exista", async () => {
    const jar = memoryCookieJar("__Host-csrf=el-token");
    const transporte = new CookieTransport({ cookieJar: jar });

    const headers = await transporte.requestHeaders("GET");

    expect(headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("agrega X-CSRF-Token en un método no seguro cuando la cookie está", async () => {
    const jar = memoryCookieJar("__Host-csrf=el-token");
    const transporte = new CookieTransport({ cookieJar: jar });

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBe("el-token");
  });

  it("sin la cookie CSRF, no falla localmente: el servidor decide", async () => {
    const transporte = new CookieTransport({ cookieJar: memoryCookieJar() });

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBeUndefined();
    expect(headers["X-Darwin-Transport"]).toBe("cookie");
  });

  // ── El default tiene que coincidir con el del servidor, en los dos entornos ──────
  //
  // `CookieConfig.name_for("csrf")` del lado Python devuelve `__Host-csrf` con `secure=True`
  // (el default, producción) y `csrf` pelado con `secure=False` (desarrollo sobre HTTP). Un
  // default que sólo acierte en uno de los dos falla en el otro sin decir nada: sin cookie no
  // se manda el header, y el servidor contesta 403 en cada write mientras los reads andan.

  it("encuentra la cookie con el prefijo __Host- que emite un despliegue con secure=True", async () => {
    const jar = memoryCookieJar("__Host-csrf=de-produccion");
    const transporte = new CookieTransport({ cookieJar: jar });

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBe("de-produccion");
  });

  it("encuentra la cookie sin prefijo que emite un despliegue con secure=False", async () => {
    const jar = memoryCookieJar("csrf=de-desarrollo");
    const transporte = new CookieTransport({ cookieJar: jar });

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBe("de-desarrollo");
  });

  it("prefiere la prefijada cuando el jar trae las dos", async () => {
    // Un navegador puede arrastrar la cookie vieja sin prefijo tras pasar el despliegue a
    // HTTPS. La que el servidor está emitiendo ahora es la prefijada, y es la que vale.
    const jar = memoryCookieJar("csrf=vieja; __Host-csrf=la-que-vale");
    const transporte = new CookieTransport({ cookieJar: jar });

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBe("la-que-vale");
  });

  it("respeta un nombre de cookie CSRF distinto del default, y lo busca prefijado también", async () => {
    const jar = memoryCookieJar("__Host-mi_csrf_propio=valor");
    const transporte = new CookieTransport({
      cookieJar: jar,
      csrfCookieName: "mi_csrf_propio",
    });

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBe("valor");
  });

  it("un nombre ya prefijado no se vuelve a prefijar", async () => {
    // Sin este caso, pasar el nombre que `name_for()` imprime daría `__Host-__Host-csrf`.
    const jar = memoryCookieJar("__Host-csrf=el-token; __Host-__Host-csrf=imposible");
    const transporte = new CookieTransport({
      cookieJar: jar,
      csrfCookieName: "__Host-csrf",
    });

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBe("el-token");
  });

  it("persist y clear son no-ops", () => {
    const transporte = new CookieTransport({ cookieJar: memoryCookieJar() });

    expect(() =>
      transporte.persist({ sessionId: "s1", expiresIn: 120, tokenType: "Bearer" }),
    ).not.toThrow();
    expect(() => transporte.clear()).not.toThrow();
  });

  it("sin cookieJar y fuera de un navegador, falla al construir en vez de a mitad de un request", () => {
    expect(() => new CookieTransport()).toThrow(/cookieJar/);
  });
});
