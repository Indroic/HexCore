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
    const jar = memoryCookieJar("darwin_csrf=el-token");
    const transporte = new CookieTransport({ cookieJar: jar });

    const headers = await transporte.requestHeaders("GET");

    expect(headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("agrega X-CSRF-Token en un método no seguro cuando la cookie está", async () => {
    const jar = memoryCookieJar("darwin_csrf=el-token");
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

  it("respeta un nombre de cookie CSRF distinto del default", async () => {
    const jar = memoryCookieJar("mi_csrf_propio=valor");
    const transporte = new CookieTransport({
      cookieJar: jar,
      csrfCookieName: "mi_csrf_propio",
    });

    const headers = await transporte.requestHeaders("POST");

    expect(headers["X-CSRF-Token"]).toBe("valor");
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
