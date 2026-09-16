import { definePlugin } from "../plugins";

/** El rol dentro de una organización. Ordenado: `owner` administra a `admin`, que administra a `member`. */
export type OrgRole = "owner" | "admin" | "member";

export interface Organization {
  id: string;
  name: string;
  slug: string;
  metadata: Record<string, unknown>;
}

export interface Member {
  userId: string;
  role: OrgRole;
  createdAt: string | null;
}

interface MemberDelServidor {
  user_id: string;
  role: OrgRole;
  created_at: string | null;
}

export interface Invitation {
  id: string;
  email: string;
  role: OrgRole;
  expiresAt: string;
}

interface InvitationDelServidor {
  id: string;
  email: string;
  role: OrgRole;
  expires_at: string;
}

/**
 * La respuesta de `invite()`. **`token` es de un solo uso**: sólo viaja acá, la misma razón por
 * la que `/auth/sign-up` devuelve el código de verificación — el framework no manda mails. En
 * producción armá el link con este valor, mandalo por mail vos mismo, y no lo muestres en la
 * interfaz.
 */
export interface InvitationIssued {
  invitation: Invitation;
  token: string;
}

export interface OrganizationApi {
  /** Crea una organización y hace `owner` al actor de la sesión actual. */
  create: (
    name: string,
    options?: { slug?: string; metadata?: Record<string, unknown> },
  ) => Promise<Organization>;
  /** Las membresías del actor — para el selector de organización. */
  mine: () => Promise<Member[]>;
  /** La organización. Exige ser miembro. */
  get: (organizationId: string) => Promise<Organization>;
  /** Actualiza nombre y metadata. **El slug no se puede cambiar** — no está en el body a propósito. */
  update: (
    organizationId: string,
    data: { name: string; metadata?: Record<string, unknown> },
  ) => Promise<Organization>;
  /** Borra la organización. Sólo un `owner`. */
  remove: (organizationId: string) => Promise<{ deleted: boolean }>;
  /** Los miembros de la organización. Exige ser miembro. */
  members: (organizationId: string) => Promise<Member[]>;
  /** Cambia el rol de un miembro. Requiere `admin` y estar por encima del rol objetivo. */
  setRole: (organizationId: string, userId: string, role: OrgRole) => Promise<Member>;
  /** Saca a un miembro. Irse uno mismo siempre se permite (salvo ser el último `owner`). */
  removeMember: (organizationId: string, userId: string) => Promise<{ removed: boolean }>;
  /** Invita por mail. Requiere `admin` o más, y no se puede invitar por encima del rol propio. */
  invite: (
    organizationId: string,
    email: string,
    role?: OrgRole,
  ) => Promise<InvitationIssued>;
  /** Las invitaciones pendientes. Requiere `admin` o más. */
  pendingInvitations: (organizationId: string) => Promise<Invitation[]>;
  /** Revoca una invitación pendiente. Requiere `admin` o más. */
  revokeInvitation: (
    organizationId: string,
    invitationId: string,
  ) => Promise<{ revoked: boolean }>;
  /** Acepta una invitación con el `token` recibido por mail. Exige sesión ya iniciada. */
  acceptInvitation: (token: string) => Promise<Member>;
}

function aMiembro(m: MemberDelServidor): Member {
  return { userId: m.user_id, role: m.role, createdAt: m.created_at };
}

function aInvitacion(i: InvitationDelServidor): Invitation {
  return { id: i.id, email: i.email, role: i.role, expiresAt: i.expires_at };
}

/** El plugin de organizaciones: multi-tenancy con roles (`owner`/`admin`/`member`) e invitaciones. */
export function organization() {
  return definePlugin({
    id: "organization",
    setup: (ctx): OrganizationApi => ({
      create: (name, options) =>
        ctx.$fetch<Organization>("/organizations", {
          method: "POST",
          body: { name, slug: options?.slug, metadata: options?.metadata ?? {} },
        }),
      mine: () =>
        ctx
          .$fetch<MemberDelServidor[]>("/organizations")
          .then((lista) => lista.map(aMiembro)),
      get: (organizationId) =>
        ctx.$fetch<Organization>(`/organizations/${encodeURIComponent(organizationId)}`),
      update: (organizationId, data) =>
        ctx.$fetch<Organization>(`/organizations/${encodeURIComponent(organizationId)}`, {
          method: "PATCH",
          body: { name: data.name, metadata: data.metadata ?? {} },
        }),
      remove: (organizationId) =>
        ctx.$fetch<{ deleted: boolean }>(
          `/organizations/${encodeURIComponent(organizationId)}`,
          { method: "DELETE" },
        ),
      members: (organizationId) =>
        ctx
          .$fetch<MemberDelServidor[]>(
            `/organizations/${encodeURIComponent(organizationId)}/members`,
          )
          .then((lista) => lista.map(aMiembro)),
      setRole: (organizationId, userId, role) =>
        ctx
          .$fetch<MemberDelServidor>(
            `/organizations/${encodeURIComponent(organizationId)}/members/${encodeURIComponent(userId)}`,
            { method: "PATCH", body: { role } },
          )
          .then(aMiembro),
      removeMember: (organizationId, userId) =>
        ctx.$fetch<{ removed: boolean }>(
          `/organizations/${encodeURIComponent(organizationId)}/members/${encodeURIComponent(userId)}`,
          { method: "DELETE" },
        ),
      invite: async (organizationId, email, role) => {
        const emitida = await ctx.$fetch<{
          invitation: InvitationDelServidor;
          token: string;
        }>(`/organizations/${encodeURIComponent(organizationId)}/invitations`, {
          method: "POST",
          body: { email, role: role ?? "member" },
        });
        return { invitation: aInvitacion(emitida.invitation), token: emitida.token };
      },
      pendingInvitations: (organizationId) =>
        ctx
          .$fetch<InvitationDelServidor[]>(
            `/organizations/${encodeURIComponent(organizationId)}/invitations`,
          )
          .then((lista) => lista.map(aInvitacion)),
      revokeInvitation: (organizationId, invitationId) =>
        ctx.$fetch<{ revoked: boolean }>(
          `/organizations/${encodeURIComponent(organizationId)}/invitations/${encodeURIComponent(invitationId)}`,
          { method: "DELETE" },
        ),
      acceptInvitation: (token) =>
        ctx
          .$fetch<MemberDelServidor>("/organizations/invitations/accept", {
            method: "POST",
            body: { token },
          })
          .then(aMiembro),
    }),
  });
}
