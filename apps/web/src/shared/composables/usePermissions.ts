/**
 * Permission checks for the UI.
 *
 * These hide affordances the principal cannot use. They are **not** the
 * security boundary — the server evaluates every request independently and this
 * code has no access to the grant table. Treating it as enforcement would be a
 * category error; treating it as decoration would produce a UI full of buttons
 * that 403.
 */

import { computed } from "vue";

import { useSessionStore } from "@/stores/session";

export function usePermissions() {
  const session = useSessionStore();

  const isAdmin = computed(() => session.user?.role === "admin");

  /** Platform capabilities are role-gated and arrive via /v1/me. */
  const can = (capability: string): boolean => session.permissions.includes(capability);

  return {
    isAdmin,
    can,
    canManageUsers: computed(() => can("platform:users")),
    canManageSettings: computed(() => can("platform:settings")),
    canReadAudit: computed(() => can("platform:audit")),
    canManageModels: computed(() => can("platform:models")),
    canManageStorage: computed(() => can("platform:storage")),
  };
}
