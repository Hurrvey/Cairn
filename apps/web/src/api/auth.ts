/**
 * Auth endpoints, typed against the generated OpenAPI schema.
 *
 * `schema.d.ts` is generated — never hand-edit it. `npm run api:check` fails the
 * build if it drifts from the server's spec, which is what stops the client and
 * server contracts diverging silently.
 */

import { api } from "@/api/client";
import type { components } from "@/api/schema";

export type LoginRequest = components["schemas"]["LoginRequest"];
export type LoginResponse = components["schemas"]["LoginResponse"];
export type CompleteSetupRequest = components["schemas"]["CompleteSetupRequest"];
export type ChangePasswordRequest = components["schemas"]["ChangePasswordRequest"];
export type MeResponse = components["schemas"]["MeResponse"];
export type UserResponse = components["schemas"]["UserResponse"];
export type PolicyResponse = components["schemas"]["PolicyResponse"];

export function login(body: LoginRequest): Promise<LoginResponse> {
  return api.post<LoginResponse>("/v1/auth/login", body);
}

export function completeInitialSetup(
  changeToken: string,
  body: CompleteSetupRequest,
): Promise<LoginResponse> {
  // The change token travels as a bearer header, never as a cookie: it must not
  // be an ambient credential that a cross-site request could ride on.
  return api.post<LoginResponse>("/v1/auth/complete-initial-setup", body, { token: changeToken });
}

export function changePassword(body: ChangePasswordRequest): Promise<void> {
  return api.post<void>("/v1/auth/change-password", body);
}

export function logout(): Promise<void> {
  return api.post<void>("/v1/auth/logout");
}

export function me(token?: string): Promise<MeResponse> {
  return api.get<MeResponse>("/v1/me", { token });
}
