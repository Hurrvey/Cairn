/**
 * Router and navigation guard.
 *
 * The guard is the UI half of FR-A-05. It is *not* the security boundary — the
 * server refuses every non-allowlisted route for a flagged principal regardless
 * of what the browser does. Its job is to make the product coherent: no route
 * should render behind a dialog the user cannot dismiss.
 */

import { createRouter, createWebHistory } from "vue-router";
import type { RouteRecordRaw } from "vue-router";

import { useSessionStore } from "@/stores/session";

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/features/auth/LoginView.vue"),
    meta: { public: true },
  },
  {
    path: "/",
    component: () => import("@/app/AppShell.vue"),
    children: [
      {
        path: "",
        name: "dashboard",
        component: () => import("@/features/dashboard/DashboardView.vue"),
      },
      {
        path: "users",
        name: "users",
        component: () => import("@/features/users/UsersView.vue"),
        meta: { capability: "platform:users" },
      },
      {
        path: "api-keys",
        name: "api-keys",
        component: () => import("@/features/api-keys/ApiKeysView.vue"),
      },
      {
        path: "audit",
        name: "audit",
        component: () => import("@/features/audit/AuditView.vue"),
        meta: { capability: "platform:audit" },
      },
      {
        path: "settings",
        name: "settings",
        component: () => import("@/features/settings/SettingsView.vue"),
        meta: { capability: "platform:settings" },
      },
    ],
  },
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: () => import("@/features/dashboard/NotFoundView.vue"),
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to) => {
  const session = useSessionStore();

  if (session.restoring) {
    await session.restore();
  }

  // A principal owing a credential change may not navigate anywhere. The dialog
  // renders over whatever route it is on, and only completing the change or
  // signing out clears it (TC-M16-02).
  if (session.mustChangePassword) {
    return to.name === "login" ? true : { name: "login" };
  }

  if (!to.meta.public && !session.isAuthenticated) {
    return { name: "login", query: to.fullPath === "/" ? {} : { next: to.fullPath } };
  }

  if (to.name === "login" && session.isAuthenticated) {
    return { name: "dashboard" };
  }

  // Capability gating is a convenience: it keeps a user off a page whose every
  // request would 403. The server remains the authority.
  const capability = to.meta.capability as string | undefined;
  if (capability && !session.permissions.includes(capability)) {
    return { name: "dashboard" };
  }

  return true;
});
