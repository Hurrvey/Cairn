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

import { i18n } from "@/app/i18n";
import { pushToast } from "@/shared/composables/useToasts";
import { useSessionStore } from "@/stores/session";

declare module "vue-router" {
  interface RouteMeta {
    public?: boolean;
    capability?: string;
    /** i18n key for the document title and the top bar. */
    titleKey?: string;
  }
}

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/features/auth/LoginView.vue"),
    meta: { public: true, titleKey: "login.submit" },
  },
  {
    path: "/",
    component: () => import("@/app/AppShell.vue"),
    children: [
      {
        path: "",
        name: "dashboard",
        component: () => import("@/features/dashboard/DashboardView.vue"),
        meta: { titleKey: "nav.dashboard" },
      },
      {
        path: "knowledge",
        name: "knowledge",
        component: () => import("@/features/knowledge/KnowledgeView.vue"),
        meta: { titleKey: "nav.knowledge" },
      },
      {
        path: "knowledge/:id",
        name: "knowledge-detail",
        component: () => import("@/features/knowledge/KnowledgeDetailView.vue"),
        meta: { titleKey: "nav.knowledge" },
      },
      {
        path: "models",
        name: "models",
        component: () => import("@/features/models/ModelsView.vue"),
        meta: { capability: "platform:models", titleKey: "nav.models" },
      },
      {
        path: "storage",
        name: "storage",
        component: () => import("@/features/storage/StorageView.vue"),
        meta: { capability: "platform:storage", titleKey: "nav.storage" },
      },
      {
        path: "api-keys",
        name: "api-keys",
        component: () => import("@/features/api-keys/ApiKeysView.vue"),
        meta: { titleKey: "nav.apiKeys" },
      },
      {
        path: "mcp-service/nav",
        name: "mcp-service",
        component: () => import("@/features/mcp/MCPServiceView.vue"),
        meta: { capability: "platform:settings", titleKey: "nav.mcpService" },
      },
      {
        path: "users",
        name: "users",
        component: () => import("@/features/users/UsersView.vue"),
        meta: { capability: "platform:users", titleKey: "nav.users" },
      },
      {
        path: "audit",
        name: "audit",
        component: () => import("@/features/audit/AuditView.vue"),
        meta: { capability: "platform:audit", titleKey: "nav.audit" },
      },
      {
        path: "settings",
        name: "settings",
        component: () => import("@/features/settings/SettingsView.vue"),
        meta: { capability: "platform:settings", titleKey: "nav.settings" },
      },
    ],
  },
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: () => import("@/features/dashboard/NotFoundView.vue"),
    meta: { public: true },
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(to, from, saved) {
    if (saved) return saved;
    if (to.path === from.path) return false;
    return { top: 0 };
  },
});

// A route chunk that fails to download (offline, or a deploy replaced the
// assets) would otherwise fail silently and leave the previous page in place.
router.onError((error) => {
  const message = String((error as Error)?.message ?? error);
  if (/dynamically imported module|Loading chunk|Importing a module script failed/i.test(message)) {
    pushToast("error", i18n.global.t("errors.pageLoad"));
  }
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
    return {
      name: "login",
      query: to.fullPath === "/" ? {} : { next: to.fullPath },
    };
  }

  if (to.name === "login" && session.isAuthenticated) {
    return { name: "dashboard" };
  }

  // Capability gating is a convenience: it keeps a user off a page whose every
  // request would 403. The server remains the authority.
  const capability = to.meta.capability;
  if (capability && !session.permissions.includes(capability)) {
    return { name: "dashboard" };
  }

  return true;
});
