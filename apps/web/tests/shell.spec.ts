/**
 * Shell behaviours: navigation grouping by capability, the command palette,
 * theme switching, and the offline banner.
 */
import { flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import AppShell from "@/app/AppShell.vue";
import { onTransport } from "@/api/client";
import { applyStoredTheme, useTheme } from "@/shared/composables/useTheme";

import { freshPinia, jsonResponse, makeRouter, mountWith, settle, signIn } from "./helpers";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
  document.documentElement.classList.remove("dark");
  localStorage.clear();
});

function mountShell(permissions: string[]) {
  const pinia = freshPinia();
  signIn(permissions.length ? "admin" : "user", permissions);
  const router = makeRouter();
  return { wrapper: mountWith(AppShell, { pinia, router, global: { stubs: { RouterView: { template: "<div />" }, ForcedCredentialDialog: { template: "<div />" } } } }), router };
}

describe("AppShell", () => {
  it("shows setup and admin groups only to capable users", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ items: [] })));
    const { wrapper } = mountShell([]);
    await flushPromises();
    expect(wrapper.find('[data-test="nav-knowledge"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="nav-api-keys"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="nav-models"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="nav-users"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("Administration");
    wrapper.unmount();

    const admin = mountShell(["platform:models", "platform:storage", "platform:users", "platform:audit", "platform:settings"]);
    await flushPromises();
    expect(admin.wrapper.find('[data-test="nav-models"]').exists()).toBe(true);
    expect(admin.wrapper.find('[data-test="nav-users"]').exists()).toBe(true);
    expect(admin.wrapper.text()).toContain("Administration");
    admin.wrapper.unmount();
  });

  it("opens the command palette with Ctrl+K and navigates on Enter", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) =>
        path.startsWith("/v1/knowledge-bases")
          ? jsonResponse({ items: [{ id: "kb_a", name: "Alpha handbook", description: null }], next_cursor: null })
          : jsonResponse({}),
      ),
    );
    const { wrapper, router } = mountShell([]);
    await flushPromises();

    await wrapper.find(".min-h-screen").trigger("keydown", { key: "k", ctrlKey: true });
    await settle();
    const input = document.body.querySelector('[data-test="palette-input"]') as HTMLInputElement | null;
    expect(input).not.toBeNull();

    input!.value = "alpha";
    input!.dispatchEvent(new Event("input", { bubbles: true }));
    await settle();
    expect(document.body.querySelector('[data-test="palette-kb:kb_a"]')).not.toBeNull();

    const palette = document.body.querySelector('[data-test="command-palette"]')!;
    palette.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await flushPromises();
    await settle();
    expect(router.currentRoute.value.path).toBe("/knowledge/kb_a");
    wrapper.unmount();
  });

  it("toggles the dark class on the root element and persists the choice", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ items: [] })));
    applyStoredTheme();
    const { wrapper } = mountShell([]);
    await flushPromises();

    await wrapper.find('[data-test="toggle-theme"]').trigger("click");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("cairn.theme")).toBe("dark");
    expect(useTheme().resolved.value).toBe("dark");

    await wrapper.find('[data-test="toggle-theme"]').trigger("click");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    wrapper.unmount();
  });

  it("shows an offline banner after a transport failure and clears it on recovery", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ items: [] })));
    const { wrapper } = mountShell([]);
    await flushPromises();
    expect(wrapper.find('[data-test="offline-banner"]').exists()).toBe(false);

    // The banner is driven by the transport listener the API client fires.
    const seen: boolean[] = [];
    const unsubscribe = onTransport((reachable) => seen.push(reachable));

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("connection refused")));
    const { request } = await import("@/api/client");
    await expect(request("/v1/anything")).rejects.toThrow();
    await flushPromises();
    expect(wrapper.find('[data-test="offline-banner"]').exists()).toBe(true);

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({})));
    await request("/v1/anything");
    await flushPromises();
    expect(wrapper.find('[data-test="offline-banner"]').exists()).toBe(false);
    expect(seen).toEqual([false, true]);
    unsubscribe();
    wrapper.unmount();
  });
});
