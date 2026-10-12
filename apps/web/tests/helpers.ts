/**
 * Shared test helpers: an i18n instance with both locales and a mount that
 * installs the plugins every view expects.
 */
import { mount, type ComponentMountingOptions } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { TooltipProvider } from "reka-ui";
import { defineComponent, h, type Component } from "vue";
import { createI18n } from "vue-i18n";
import { createMemoryHistory, createRouter, type Router, type RouteRecordRaw } from "vue-router";

import enUS from "@/locales/en-US.json";
import zhCN from "@/locales/zh-CN.json";
import { useSessionStore } from "@/stores/session";

export function makeI18n(locale: "en-US" | "zh-CN" = "en-US") {
  return createI18n({
    legacy: false,
    locale,
    fallbackLocale: "en-US",
    messages: { "en-US": enUS, "zh-CN": zhCN },
    missingWarn: false,
    fallbackWarn: false,
  });
}

const NAMED_ROUTES: RouteRecordRaw[] = [
  { path: "/", name: "dashboard", component: { template: "<div />" }, meta: { titleKey: "nav.dashboard" } },
  { path: "/knowledge", name: "knowledge", component: { template: "<div />" } },
  { path: "/knowledge/:id", name: "knowledge-detail", component: { template: "<div />" } },
  { path: "/models", name: "models", component: { template: "<div />" } },
  { path: "/model-console", name: "model-console", component: { template: "<div />" } },
  { path: "/storage", name: "storage", component: { template: "<div />" } },
  { path: "/api-keys", name: "api-keys", component: { template: "<div />" } },
  { path: "/users", name: "users", component: { template: "<div />" } },
  { path: "/audit", name: "audit", component: { template: "<div />" } },
  { path: "/settings", name: "settings", component: { template: "<div />" } },
  { path: "/mcp-service/nav", name: "mcp-service", component: { template: "<div />" } },
  { path: "/login", name: "login", component: { template: "<div />" } },
];

/** A memory router that knows every named route the shell links to; the
 *  routes passed in replace the stubs with the same path. */
export function makeRouter(routes: RouteRecordRaw[] = []): Router {
  const paths = new Set(routes.map((route) => route.path));
  return createRouter({
    history: createMemoryHistory(),
    routes: [...routes, ...NAMED_ROUTES.filter((route) => !paths.has(route.path))],
  });
}

export function freshPinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

export function signIn(role: "admin" | "user" = "admin", permissions: string[] = []) {
  const session = useSessionStore();
  session.user = {
    id: role === "admin" ? "usr_admin" : "usr_user",
    username: role === "admin" ? "dana.ops" : "wei",
    email: null,
    display_name: null,
    role,
    must_change_password: false,
    is_active: true,
    last_login_at: null,
    created_at: "2026-09-01T00:00:00Z",
  };
  session.permissions = permissions;
  session.restoring = false;
  return session;
}

/** Mount with a TooltipProvider so components using Tooltip render outside the shell. */
export function mountWith<T extends Component>(
  component: T,
  options: ComponentMountingOptions<T> & { pinia?: Pinia; router?: Router } = {},
) {
  const { pinia, router, ...rest } = options;
  const plugins = [pinia ?? freshPinia(), makeI18n(), ...(router ? [router] : []), ...((rest.global?.plugins as unknown[]) ?? [])];
  const Wrapper = defineComponent({
    setup(_, { attrs }) {
      return () => h(TooltipProvider, null, { default: () => h(component as Component, attrs) });
    },
  });
  return mount(Wrapper, {
    ...rest,
    global: { ...rest.global, plugins },
    attachTo: rest.attachTo ?? document.body,
  } as ComponentMountingOptions<typeof Wrapper>);
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Flush microtasks a few times so chained awaits settle in tests. */
export async function settle(times = 3): Promise<void> {
  for (let index = 0; index < times; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}
