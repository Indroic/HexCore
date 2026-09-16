import { describe, expect, it } from "vitest";
import { createDarwinClient } from "../../src/core/client";
import { organization } from "../../src/plugins/organization";
import { BearerTransport } from "../../src/transport/bearer";
import { memoryStorage } from "../../src/transport/storage";
import { jsonResponse } from "./helpers/fake-fetch";

interface Estado {
  org: { id: string; name: string; slug: string; metadata: Record<string, unknown> };
  members: Array<{ user_id: string; role: string; created_at: string | null }>;
  invitations: Array<{ id: string; email: string; role: string; expires_at: string }>;
}

function fakeServer() {
  const estado: Estado = {
    org: { id: "org-1", name: "Acme", slug: "acme", metadata: {} },
    members: [{ user_id: "u1", role: "owner", created_at: "2026-01-01T00:00:00Z" }],
    invitations: [],
  };
  let contadorInvitaciones = 0;

  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body
      ? (JSON.parse(String(init.body)) as Record<string, unknown>)
      : {};

    if (path === "/organizations" && method === "POST") {
      return jsonResponse({
        status: 201,
        body: {
          id: "org-1",
          name: body.name,
          slug: body.slug ?? "acme",
          metadata: body.metadata ?? {},
        },
      });
    }

    if (path === "/organizations" && method === "GET") {
      return jsonResponse({ body: estado.members });
    }

    if (path === "/organizations/org-1" && method === "GET") {
      return jsonResponse({ body: estado.org });
    }

    if (path === "/organizations/org-1" && method === "PATCH") {
      estado.org.name = body.name as string;
      estado.org.metadata = (body.metadata as Record<string, unknown>) ?? {};
      return jsonResponse({ body: estado.org });
    }

    if (path === "/organizations/org-1" && method === "DELETE") {
      return jsonResponse({ body: { deleted: true } });
    }

    if (path === "/organizations/org-1/members" && method === "GET") {
      return jsonResponse({ body: estado.members });
    }

    if (path === "/organizations/org-1/members/u2" && method === "PATCH") {
      return jsonResponse({
        body: { user_id: "u2", role: body.role, created_at: "2026-01-02T00:00:00Z" },
      });
    }

    if (path === "/organizations/org-1/members/u2" && method === "DELETE") {
      return jsonResponse({ body: { removed: true } });
    }

    if (path === "/organizations/org-1/invitations" && method === "POST") {
      contadorInvitaciones += 1;
      const invitacion = {
        id: `inv-${contadorInvitaciones}`,
        email: body.email as string,
        role: (body.role as string) ?? "member",
        expires_at: "2026-02-01T00:00:00Z",
      };
      estado.invitations.push(invitacion);
      return jsonResponse({
        status: 201,
        body: { invitation: invitacion, token: `token-${contadorInvitaciones}` },
      });
    }

    if (path === "/organizations/org-1/invitations" && method === "GET") {
      return jsonResponse({ body: estado.invitations });
    }

    if (path === "/organizations/org-1/invitations/inv-1" && method === "DELETE") {
      estado.invitations = estado.invitations.filter((i) => i.id !== "inv-1");
      return jsonResponse({ body: { revoked: true } });
    }

    if (path === "/organizations/invitations/accept" && method === "POST") {
      if (body.token !== "token-1") {
        return jsonResponse({
          status: 404,
          body: { detail: "invitación no encontrada", error: "NotFoundError" },
        });
      }
      return jsonResponse({
        body: { user_id: "u3", role: "member", created_at: "2026-01-03T00:00:00Z" },
      });
    }

    throw new Error(`ruta no simulada: ${method} ${path}`);
  }) as typeof fetch;

  return { fetchImpl };
}

function nuevoCliente(fetchImpl: typeof fetch) {
  return createDarwinClient({
    baseUrl: "https://api.test",
    transport: new BearerTransport({ storage: memoryStorage() }),
    fetch: fetchImpl,
    hydrateOnCreate: false,
    plugins: [organization()],
  });
}

describe("plugin organization", () => {
  it("create() y get() delegan en las rutas correspondientes", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const creada = await client.organization.create("Acme");
    expect(creada).toEqual({ id: "org-1", name: "Acme", slug: "acme", metadata: {} });

    await expect(client.organization.get("org-1")).resolves.toEqual(creada);
  });

  it("mine() y members() traen membresías en camelCase", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.organization.mine()).resolves.toEqual([
      { userId: "u1", role: "owner", createdAt: "2026-01-01T00:00:00Z" },
    ]);
    await expect(client.organization.members("org-1")).resolves.toEqual([
      { userId: "u1", role: "owner", createdAt: "2026-01-01T00:00:00Z" },
    ]);
  });

  it("update() y remove() delegan en las rutas correspondientes", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const actualizada = await client.organization.update("org-1", { name: "Acme Corp" });
    expect(actualizada.name).toBe("Acme Corp");

    await expect(client.organization.remove("org-1")).resolves.toEqual({ deleted: true });
  });

  it("setRole() y removeMember() delegan en las rutas correspondientes", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await expect(client.organization.setRole("org-1", "u2", "admin")).resolves.toEqual({
      userId: "u2",
      role: "admin",
      createdAt: "2026-01-02T00:00:00Z",
    });
    await expect(client.organization.removeMember("org-1", "u2")).resolves.toEqual({
      removed: true,
    });
  });

  it("invite() trae el token de un solo uso, y pendingInvitations() lo omite", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    const emitida = await client.organization.invite("org-1", "nuevo@test.com", "member");
    expect(emitida.token).toBe("token-1");
    expect(emitida.invitation).toMatchObject({ email: "nuevo@test.com", role: "member" });

    const pendientes = await client.organization.pendingInvitations("org-1");
    expect(pendientes).toEqual([emitida.invitation]);
    expect(pendientes[0]).not.toHaveProperty("token");
  });

  it("revokeInvitation() y acceptInvitation() delegan en las rutas correspondientes", async () => {
    const { fetchImpl } = fakeServer();
    const client = nuevoCliente(fetchImpl);

    await client.organization.invite("org-1", "nuevo@test.com");
    await expect(client.organization.revokeInvitation("org-1", "inv-1")).resolves.toEqual({
      revoked: true,
    });

    await expect(
      client.organization.acceptInvitation("token-invalido"),
    ).rejects.toMatchObject({
      code: "NotFoundError",
    });
  });
});
