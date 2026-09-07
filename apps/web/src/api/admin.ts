/**
 * Typed API surface for the admin screens.
 *
 * Types come from the generated schema, so a backend contract change surfaces
 * as a TypeScript error here rather than as a runtime surprise in a browser.
 */

import { api, request } from "@/api/client";
import type { components } from "@/api/schema";

type S = components["schemas"];

// --- users -------------------------------------------------------------------

export type UserResponse = S["UserResponse"];
export type CreateUserRequest = S["CreateUserRequest"];
export type UpdateUserRequest = S["UpdateUserRequest"];
export type CreatedUserResponse = S["CreatedUserResponse"];

export const users = {
  list: (params: { limit?: number; cursor?: string } = {}) =>
    api.get<UserResponse[]>(`/v1/users${query(params)}`),
  create: (body: CreateUserRequest) => api.post<CreatedUserResponse>("/v1/users", body),
  get: (id: string) => api.get<UserResponse>(`/v1/users/${id}`),
  update: (id: string, body: UpdateUserRequest) =>
    request<UserResponse>(`/v1/users/${id}`, { method: "PATCH", body }),
  remove: (id: string) => request<void>(`/v1/users/${id}`, { method: "DELETE" }),
  forcePasswordChange: (id: string) =>
    api.post<{ user_id: string; effective_at: string }>(
      `/v1/users/${id}/force-password-change`,
    ),
};

// --- grants ------------------------------------------------------------------

export type GrantResponse = S["GrantResponse"];
export type CreateGrantRequest = S["CreateGrantRequest"];
export type EffectivePermissionsResponse = S["EffectivePermissionsResponse"];

export const grants = {
  forUser: (userId: string) => api.get<GrantResponse[]>(`/v1/users/${userId}/grants`),
  effective: (userId: string) =>
    api.get<EffectivePermissionsResponse>(`/v1/users/${userId}/permissions`),
  create: (body: CreateGrantRequest) => api.post<GrantResponse>("/v1/grants", body),
  revoke: (id: string) => request<void>(`/v1/grants/${id}`, { method: "DELETE" }),
  catalogue: () => api.get<Record<string, string[]>>("/v1/workspace/permissions"),
};

// --- api keys ----------------------------------------------------------------

export type ApiKeyResponse = S["ApiKeyResponse"];
export type CreateApiKeyRequest = S["CreateApiKeyRequest"];
export type ApiKeyCreatedResponse = S["ApiKeyCreatedResponse"];

export const apiKeys = {
  list: (allUsers = false) =>
    api.get<ApiKeyResponse[]>(`/v1/api-keys${allUsers ? "?all_users=true" : ""}`),
  create: (body: CreateApiKeyRequest) => api.post<ApiKeyCreatedResponse>("/v1/api-keys", body),
  revoke: (id: string) => request<void>(`/v1/api-keys/${id}`, { method: "DELETE" }),
};

// --- audit -------------------------------------------------------------------

export interface AuditEntry {
  id: number;
  at: string;
  actor_type: string;
  actor_id: string | null;
  actor_label: string;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  outcome: "success" | "failure" | "denied";
  ip: string | null;
  request_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  detail: Record<string, unknown>;
}

export interface AuditPage {
  items: AuditEntry[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface AuditFilters {
  [key: string]: unknown;
  actor_id?: string;
  action?: string;
  action_prefix?: string;
  resource_type?: string;
  outcome?: string;
  since?: string;
  until?: string;
  limit?: number;
  cursor?: string;
}

export const audit = {
  query: (filters: AuditFilters = {}) => api.get<AuditPage>(`/v1/audit-log${query(filters)}`),
  exportUrl: (filters: Pick<AuditFilters, "since" | "until"> = {}) =>
    `/v1/audit-log/export${query(filters)}`,
};

// --- settings & queues -------------------------------------------------------

export type WorkspaceSettings = S["WorkspaceSettings"];
export type SettingsResponse = S["SettingsResponse"];

export const settings = {
  get: () => api.get<SettingsResponse>("/v1/settings"),
  update: (patch: Record<string, unknown>) =>
    request<SettingsResponse>("/v1/settings", { method: "PATCH", body: { settings: patch } }),
};

export interface QueueStat {
  queue: string;
  ready: number;
  oldest_ready_age_seconds: number;
}

export const queues = {
  stats: () => api.get<{ queues: QueueStat[] }>("/v1/tasks/queues"),
};

// --- helpers -----------------------------------------------------------------

function query(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}
