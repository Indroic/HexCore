import { describe, expect, it, vi } from "vitest";
import { DarwinError } from "../../src/core/errors";
import type { SessionResponse } from "../../src/core/types";
import { createRefreshController } from "../../src/session/refresh";

function sesion(): SessionResponse {
  return { session_id: "s1", expires_in: 120, token_type: "Bearer" };
}

describe("createRefreshController — single-flight", () => {
  it("N llamadas concurrentes disparan un solo doRefresh", async () => {
    let resolverDoRefresh!: (value: SessionResponse) => void;
    const doRefresh = vi.fn(
      () =>
        new Promise<SessionResponse>((resolve) => {
          resolverDoRefresh = resolve;
        }),
    );
    const onSuccess = vi.fn();
    const onFailure = vi.fn();

    const controller = createRefreshController({ doRefresh, onSuccess, onFailure });

    const N = 10;
    const promesas = Array.from({ length: N }, () => controller.refresh());

    expect(doRefresh).toHaveBeenCalledTimes(1);

    resolverDoRefresh(sesion());
    await Promise.all(promesas);

    expect(doRefresh).toHaveBeenCalledTimes(1);
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  it("un refresh nuevo después de que el anterior terminó dispara doRefresh de nuevo", async () => {
    const doRefresh = vi.fn(async () => sesion());
    const controller = createRefreshController({
      doRefresh,
      onSuccess: vi.fn(),
      onFailure: vi.fn(),
    });

    await controller.refresh();
    await controller.refresh();

    expect(doRefresh).toHaveBeenCalledTimes(2);
  });

  it("tras un fallo DEFINITIVO del servidor, la sesión queda marcada muerta: no vuelve a pegarle a la red", async () => {
    const doRefresh = vi.fn(async () => {
      throw new DarwinError({
        code: "TokenRevokedError",
        status: 401,
        detail: "Se detectó el reuso de un token de refresco.",
      });
    });
    const onFailure = vi.fn();
    const controller = createRefreshController({
      doRefresh,
      onSuccess: vi.fn(),
      onFailure,
    });

    await expect(controller.refresh()).rejects.toThrow();
    expect(doRefresh).toHaveBeenCalledTimes(1);
    expect(onFailure).toHaveBeenCalledTimes(1);

    // Sin la marca, esto dispararía un segundo doRefresh contra un refresh que ya se sabe caído.
    await expect(controller.refresh()).rejects.toThrow(/marked dead/);
    expect(doRefresh).toHaveBeenCalledTimes(1);
  });

  it("tras un fallo TRANSITORIO (red), la sesión NO queda muerta: el próximo refresh reintenta solo", async () => {
    const doRefresh = vi.fn(async () => {
      throw new DarwinError({
        code: "NetworkError",
        status: null,
        detail:
          "The request never completed (no connection, CORS, or the server did not answer).",
      });
    });
    const onFailure = vi.fn();
    const controller = createRefreshController({
      doRefresh,
      onSuccess: vi.fn(),
      onFailure,
    });

    await expect(controller.refresh()).rejects.toThrow();
    expect(doRefresh).toHaveBeenCalledTimes(1);
    expect(onFailure).toHaveBeenCalledTimes(1);

    // Sin necesitar markAlive(): un blip de red no marca la sesión muerta, así que el próximo
    // refresh vuelve a pegarle a la red en vez de rechazar con "marked dead".
    await expect(controller.refresh()).rejects.toThrow();
    expect(doRefresh).toHaveBeenCalledTimes(2);
  });

  it("markAlive() limpia la marca de muerta", async () => {
    const doRefresh = vi.fn<() => Promise<SessionResponse>>(async () => {
      throw new DarwinError({
        code: "TokenRevokedError",
        status: 401,
        detail: "primero falla",
      });
    });
    const controller = createRefreshController({
      doRefresh,
      onSuccess: vi.fn(),
      onFailure: vi.fn(),
    });

    await expect(controller.refresh()).rejects.toThrow();
    controller.markAlive();

    doRefresh.mockImplementationOnce(async () => sesion());
    await expect(controller.refresh()).resolves.toBeUndefined();
    expect(doRefresh).toHaveBeenCalledTimes(2);
  });
});
