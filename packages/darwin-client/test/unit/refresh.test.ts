import { describe, expect, it, vi } from "vitest";
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

  it("tras un fallo, la sesión queda marcada muerta: no vuelve a pegarle a la red", async () => {
    const doRefresh = vi.fn(async () => {
      throw new Error("refresh token vencido");
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

  it("markAlive() limpia la marca de muerta", async () => {
    const doRefresh = vi.fn<() => Promise<SessionResponse>>(async () => {
      throw new Error("primero falla");
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
