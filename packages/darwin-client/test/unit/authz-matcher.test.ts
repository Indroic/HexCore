import { describe, expect, it } from "vitest";
import { compilePermissions, grantsPermission } from "../../src/authz/matcher";

describe("compilePermissions/grantsPermission", () => {
  it("concede una coincidencia exacta", () => {
    const compilado = compilePermissions(["invoice.read"]);
    expect(grantsPermission(compilado, "invoice.read")).toBe(true);
    expect(grantsPermission(compilado, "invoice.approve")).toBe(false);
  });

  it("un comodín de prefijo concede cualquier acción de ese recurso", () => {
    const compilado = compilePermissions(["users.*"]);
    expect(grantsPermission(compilado, "users.invite")).toBe(true);
    expect(grantsPermission(compilado, "users.delete")).toBe(true);
  });

  it("un comodín de prefijo NO concede el nodo pelado", () => {
    const compilado = compilePermissions(["users.*"]);
    expect(grantsPermission(compilado, "users")).toBe(false);
  });

  it("el comodín global concede cualquier cosa", () => {
    const compilado = compilePermissions(["*"]);
    expect(grantsPermission(compilado, "invoice.approve")).toBe(true);
    expect(grantsPermission(compilado, "cualquier.cosa")).toBe(true);
    expect(grantsPermission(compilado, "*")).toBe(true);
  });

  it("un conjunto vacío no concede nada", () => {
    const compilado = compilePermissions([]);
    expect(grantsPermission(compilado, "invoice.read")).toBe(false);
    expect(grantsPermission(compilado, "*")).toBe(false);
  });

  it("prueba los prefijos de más específico a más general en una jerarquía profunda", () => {
    const compilado = compilePermissions(["a.b.*"]);
    expect(grantsPermission(compilado, "a.b.c")).toBe(true);
    expect(grantsPermission(compilado, "a.c.d")).toBe(false);
  });

  it("un comodín más general concede aunque el pedido sea más profundo", () => {
    const compilado = compilePermissions(["a.*"]);
    expect(grantsPermission(compilado, "a.b.c")).toBe(true);
  });

  it("combina exactos y comodines en el mismo conjunto", () => {
    const compilado = compilePermissions(["users.*", "invoice.read"]);
    expect(grantsPermission(compilado, "users.invite")).toBe(true);
    expect(grantsPermission(compilado, "invoice.read")).toBe(true);
    expect(grantsPermission(compilado, "invoice.approve")).toBe(false);
  });
});
