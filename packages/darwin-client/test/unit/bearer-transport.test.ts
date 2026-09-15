import { describe, expect, it } from "vitest";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";

describe("BearerTransport", () => {
  it("manda X-Darwin-Transport sin Authorization cuando no hay token", async () => {
    const transporte = new BearerTransport({ storage: memoryStorage() });

    const headers = await transporte.requestHeaders("GET");

    expect(headers["X-Darwin-Transport"]).toBe("bearer");
    expect(headers.Authorization).toBeUndefined();
  });

  it("persist guarda el access y el refresh token", async () => {
    const storage = memoryStorage();
    const transporte = new BearerTransport({ storage });

    await transporte.persist({
      sessionId: "s1",
      expiresIn: 120,
      tokenType: "Bearer",
      accessToken: "access-1",
      refreshToken: "refresh-1",
    });

    const headers = await transporte.requestHeaders("GET");
    expect(headers.Authorization).toBe("Bearer access-1");
    expect(await transporte.currentRefreshToken()).toBe("refresh-1");
  });

  it("clear borra los dos tokens", async () => {
    const storage = memoryStorage();
    const transporte = new BearerTransport({ storage });

    await transporte.persist({
      sessionId: "s1",
      expiresIn: 120,
      tokenType: "Bearer",
      accessToken: "access-1",
      refreshToken: "refresh-1",
    });
    await transporte.clear();

    const headers = await transporte.requestHeaders("GET");
    expect(headers.Authorization).toBeUndefined();
    expect(await transporte.currentRefreshToken()).toBeNull();
  });

  it("persist sin refreshToken (camino de cookie no aplica acá, pero la forma es opcional) no rompe", async () => {
    const transporte = new BearerTransport({ storage: memoryStorage() });

    await expect(
      transporte.persist({ sessionId: "s1", expiresIn: 120, tokenType: "Bearer" }),
    ).resolves.toBeUndefined();
  });

  it("usa memoryStorage() por defecto si no se pasa storage", async () => {
    const transporte = new BearerTransport();

    await transporte.persist({
      sessionId: "s1",
      expiresIn: 120,
      tokenType: "Bearer",
      accessToken: "a",
    });

    const headers = await transporte.requestHeaders("GET");
    expect(headers.Authorization).toBe("Bearer a");
  });
});
