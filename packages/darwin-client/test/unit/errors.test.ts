import { describe, expect, it } from "vitest";
import {
  DarwinError,
  darwinErrorFromResponse,
  isRefreshable,
  isSessionDead,
  isTwoFactorRequired,
  parseWwwAuthenticate,
} from "../../src/core/errors";
import { darwinErrorResponse, jsonResponse } from "./helpers/fake-fetch";

describe("darwinErrorFromResponse", () => {
  it("parsea el envelope de Darwin", async () => {
    const response = darwinErrorResponse(401, "TokenExpiredError");
    const error = await darwinErrorFromResponse(response);

    expect(error).toBeInstanceOf(DarwinError);
    expect(error.code).toBe("TokenExpiredError");
    expect(error.status).toBe(401);
    expect(error.detail).toBe("detalle de TokenExpiredError");
  });

  it("preserva claves extra del payload (el challenge de 2FA)", async () => {
    const response = darwinErrorResponse(401, "TwoFactorRequiredError", {
      challenge: "abc123",
    });
    const error = await darwinErrorFromResponse(response);

    expect(error.payload.challenge).toBe("abc123");
  });

  it("da NonJsonResponse cuando el cuerpo no es JSON", async () => {
    const response = new Response("<html>502 Bad Gateway</html>", { status: 502 });
    const error = await darwinErrorFromResponse(response);

    expect(error.code).toBe("NonJsonResponse");
    expect(error.detail).toContain("502 Bad Gateway");
  });

  it("da NonJsonResponse cuando el JSON no trae el envelope de Darwin", async () => {
    const response = jsonResponse({ status: 500, body: { message: "algo distinto" } });
    const error = await darwinErrorFromResponse(response);

    expect(error.code).toBe("NonJsonResponse");
  });

  it("preserva un código de error que el cliente todavía no conoce", async () => {
    const response = darwinErrorResponse(403, "AlgoNuevoQueElBackendAgrego");
    const error = await darwinErrorFromResponse(response);

    expect(error.code).toBe("AlgoNuevoQueElBackendAgrego");
  });

  it("parsea WWW-Authenticate cuando viene", async () => {
    const response = jsonResponse({
      status: 401,
      body: { detail: "sin credencial", error: "UnauthenticatedError" },
      headers: { "WWW-Authenticate": 'Bearer realm="hexcore", error="invalid_token"' },
    });
    const error = await darwinErrorFromResponse(response);

    expect(error.wwwAuthenticate?.scheme).toBe("Bearer");
    expect(error.wwwAuthenticate?.params.realm).toBe("hexcore");
    expect(error.wwwAuthenticate?.params.error).toBe("invalid_token");
  });
});

describe("predicados", () => {
  it("isRefreshable es true sólo para 401 con un código refrescable", () => {
    const refrescable = new DarwinError({
      code: "TokenExpiredError",
      status: 401,
      detail: "x",
    });
    const noRefrescablePorCodigo = new DarwinError({
      code: "InvalidCredentialsError",
      status: 401,
      detail: "x",
    });
    const noRefrescablePorStatus = new DarwinError({
      code: "TokenExpiredError",
      status: 403,
      detail: "x",
    });

    expect(isRefreshable(refrescable)).toBe(true);
    expect(isRefreshable(noRefrescablePorCodigo)).toBe(false);
    expect(isRefreshable(noRefrescablePorStatus)).toBe(false);
    expect(isRefreshable(new Error("no es un DarwinError"))).toBe(false);
  });

  it("isSessionDead sólo para TokenRevokedError", () => {
    const revocado = new DarwinError({
      code: "TokenRevokedError",
      status: 401,
      detail: "x",
    });
    const vencido = new DarwinError({
      code: "TokenExpiredError",
      status: 401,
      detail: "x",
    });

    expect(isSessionDead(revocado)).toBe(true);
    expect(isSessionDead(vencido)).toBe(false);
  });

  it("isTwoFactorRequired exige el challenge en el payload", () => {
    const conChallenge = new DarwinError({
      code: "TwoFactorRequiredError",
      status: 401,
      detail: "x",
      payload: { detail: "x", error: "TwoFactorRequiredError", challenge: "abc" },
    });
    const sinChallenge = new DarwinError({
      code: "TwoFactorRequiredError",
      status: 401,
      detail: "x",
      payload: { detail: "x", error: "TwoFactorRequiredError" },
    });

    expect(isTwoFactorRequired(conChallenge)).toBe(true);
    if (isTwoFactorRequired(conChallenge)) {
      // Angostado: TypeScript ya sabe que payload.challenge es string acá.
      expect(conChallenge.payload.challenge.length).toBeGreaterThan(0);
    }
    expect(isTwoFactorRequired(sinChallenge)).toBe(false);
  });
});

describe("parseWwwAuthenticate", () => {
  it("devuelve undefined sin header", () => {
    expect(parseWwwAuthenticate(null)).toBeUndefined();
  });

  it("parsea scheme y params", () => {
    const parsed = parseWwwAuthenticate('Bearer realm="hexcore", error="invalid_token"');
    expect(parsed?.scheme).toBe("Bearer");
    expect(parsed?.params).toEqual({ realm: "hexcore", error: "invalid_token" });
  });
});
