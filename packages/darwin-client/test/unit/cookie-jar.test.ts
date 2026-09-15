import { describe, expect, it } from "vitest";
import { memoryCookieJar } from "../../src/transport/cookie-jar";

describe("memoryCookieJar", () => {
  it("parsea un header Cookie con varias cookies", async () => {
    const jar = memoryCookieJar("darwin_sid=abc123; darwin_csrf=xyz789; other=1");

    expect(await jar.get("darwin_csrf")).toBe("xyz789");
    expect(await jar.get("darwin_sid")).toBe("abc123");
  });

  it("decodifica valores URL-encoded", async () => {
    const jar = memoryCookieJar(`token=${encodeURIComponent("a b/c")}`);

    expect(await jar.get("token")).toBe("a b/c");
  });

  it("devuelve undefined para una cookie que no está", async () => {
    const jar = memoryCookieJar("otra=1");

    expect(await jar.get("darwin_csrf")).toBeUndefined();
  });

  it("un header vacío no rompe", async () => {
    const jar = memoryCookieJar();

    expect(await jar.get("cualquiera")).toBeUndefined();
  });
});
