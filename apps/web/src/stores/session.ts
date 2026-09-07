/**
 * Session state — a mirror of the server's state machine, never the authority.
 *
 * The three states correspond exactly to what the server will allow:
 *
 *   anonymous          -> only /v1/auth/login
 *   credentialChange   -> only complete-initial-setup, logout, /v1/me  (FR-A-05)
 *   authenticated      -> everything the principal is granted
 *
 * The change token lives in memory only. Persisting it to localStorage would
 * survive a reload, but that is precisely the wrong behaviour: the requirement
 * is durable server-side (FR-A-06), so a reload should send the user back
 * through login and mint a fresh, short-lived token.
 */

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import * as authApi from "@/api/auth";
import type { MeResponse, PolicyResponse, UserResponse } from "@/api/auth";

export type SessionState = "anonymous" | "credentialChange" | "authenticated";

export const useSessionStore = defineStore("session", () => {
  const user = ref<UserResponse | null>(null);
  const mustChangePassword = ref(false);
  const changeToken = ref<string | null>(null);
  const changeReason = ref<string | null>(null);
  const policy = ref<PolicyResponse | null>(null);
  const permissions = ref<string[]>([]);
  const restoring = ref(true);

  const state = computed<SessionState>(() => {
    if (mustChangePassword.value) return "credentialChange";
    return user.value ? "authenticated" : "anonymous";
  });

  const isAuthenticated = computed(() => state.value === "authenticated");

  function reset(): void {
    user.value = null;
    mustChangePassword.value = false;
    changeToken.value = null;
    changeReason.value = null;
    policy.value = null;
    permissions.value = [];
  }

  /** Called by the global interceptor when ANY request returns 403
   *  PASSWORD_CHANGE_REQUIRED — see FR-P-02 / TC-M16-03. */
  function requireCredentialChange(): void {
    mustChangePassword.value = true;
  }

  async function signIn(username: string, password: string): Promise<SessionState> {
    const result = await authApi.login({ username, password });
    user.value = result.user;

    if (result.status === "password_change_required") {
      mustChangePassword.value = true;
      changeToken.value = result.change_token ?? null;
      changeReason.value = result.reason ?? null;
      policy.value = result.policy ?? null;
    } else {
      mustChangePassword.value = false;
      changeToken.value = null;
      await loadMe();
    }
    return state.value;
  }

  async function completeSetup(input: {
    currentPassword: string;
    newPassword: string;
    confirmPassword: string;
    newUsername?: string | undefined;
  }): Promise<void> {
    if (!changeToken.value) {
      throw new Error("No setup token — sign in again.");
    }
    const result = await authApi.completeInitialSetup(changeToken.value, {
      current_password: input.currentPassword,
      new_password: input.newPassword,
      confirm_password: input.confirmPassword,
      new_username: input.newUsername ?? null,
    });

    // Only on success does the server issue a real session.
    user.value = result.user;
    mustChangePassword.value = false;
    changeToken.value = null;
    changeReason.value = null;
    policy.value = null;
    await loadMe();
  }

  async function loadMe(token?: string): Promise<MeResponse | null> {
    try {
      const result = await authApi.me(token);
      user.value = result.user;
      permissions.value = result.permissions;
      mustChangePassword.value = result.must_change_password;
      return result;
    } catch {
      return null;
    }
  }

  /** Boot-time restore: is there already a valid cookie session? */
  async function restore(): Promise<void> {
    restoring.value = true;
    try {
      const result = await loadMe();
      if (!result) reset();
    } finally {
      restoring.value = false;
    }
  }

  async function signOut(): Promise<void> {
    try {
      await authApi.logout();
    } finally {
      reset();
    }
  }

  return {
    user,
    mustChangePassword,
    changeToken,
    changeReason,
    policy,
    permissions,
    restoring,
    state,
    isAuthenticated,
    signIn,
    completeSetup,
    completeSetupPolicy: policy,
    loadMe,
    restore,
    signOut,
    reset,
    requireCredentialChange,
  };
});
