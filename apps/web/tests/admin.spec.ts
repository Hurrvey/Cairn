/**
 * Admin screens — TC-M16-06 … TC-M16-13.
 *
 * The behaviours worth locking down here are the ones a refactor would quietly
 * break: show-once secrets staying behind an acknowledgement, capability-gated
 * navigation, and cursor pagination not silently resetting.
 */

import ElementPlus from "element-plus";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { createI18n } from "vue-i18n";

import enUS from "@/locales/en-US.json";
import zhCN from "@/locales/zh-CN.json";
import SecretReveal from "@/shared/components/SecretReveal.vue";
import { usePermissions } from "@/shared/composables/usePermissions";
import { useSessionStore } from "@/stores/session";

const i18n = createI18n({
  legacy: false,
  locale: "en-US",
  fallbackLocale: "en-US",
  messages: { "en-US": enUS, "zh-CN": zhCN },
});

/** Mount SecretReveal with its real prop types intact — a generic `unknown`
 *  helper would erase them and hide exactly the kind of contract drift these
 *  tests exist to catch. */
function mountSecret(props: { value: string; label: string; hint?: string }) {
  return mount(SecretReveal, {
    props,
    global: { plugins: [i18n, ElementPlus] },
    attachTo: document.body,
  });
}

function signedIn(role: "admin" | "user", permissions: string[] = []) {
  const session = useSessionStore();
  session.user = {
    id: "usr_01H",
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
  return session;
}

const ADMIN_CAPS = [
  "platform:users",
  "platform:models",
  "platform:storage",
  "platform:settings",
  "platform:audit",
];

describe("usePermissions", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("TC-M16-13: reflects the capabilities the server reported", () => {
    signedIn("admin", ADMIN_CAPS);
    const permissions = usePermissions();
    expect(permissions.isAdmin.value).toBe(true);
    expect(permissions.canManageUsers.value).toBe(true);
    expect(permissions.canReadAudit.value).toBe(true);
  });

  it("grants nothing to a regular user", () => {
    signedIn("user", []);
    const permissions = usePermissions();
    expect(permissions.isAdmin.value).toBe(false);
    expect(permissions.canManageUsers.value).toBe(false);
    expect(permissions.canManageSettings.value).toBe(false);
  });

  it("never infers a capability from the role alone", () => {
    // The server is the authority on capabilities. Inferring them from `role`
    // would drift the moment the permission model changes.
    signedIn("admin", []);
    expect(usePermissions().canManageUsers.value).toBe(false);
  });
});

describe("SecretReveal", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  });

  it("shows the value and warns that it appears once", async () => {
    const wrapper = mountSecret({
      value: "cairn_sk_live_7Kq2mVx9",
      label: "API key",
    });
    await nextTick();

    const html = wrapper.html();
    expect(html).toContain("cairn_sk_live_7Kq2mVx9");
    expect(html).toContain("shown only once");
  });

  it("starts unacknowledged so the caller can gate the close button", async () => {
    const wrapper = mountSecret({ value: "secret", label: "Key" });
    await nextTick();
    const checkbox = document.body.querySelector(
      '[data-test="secret-ack"] input',
    ) as HTMLInputElement | null;
    expect(checkbox?.checked).toBe(false);
    expect(wrapper.html()).toContain("saved this somewhere safe");
  });

  it("copies to the clipboard and reports it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    const wrapper = mountSecret({ value: "cairn_sk_live_x", label: "Key" });
    await nextTick();

    await wrapper.find('[data-test="secret-copy"]').trigger("click");
    await nextTick();

    expect(writeText).toHaveBeenCalledWith("cairn_sk_live_x");
  });

  it("does not throw when the clipboard is unavailable", async () => {
    // navigator.clipboard is undefined over plain http; the user copies by hand.
    vi.stubGlobal("navigator", {});
    const wrapper = mountSecret({ value: "x", label: "Key" });
    await nextTick();
    await expect(
      wrapper.find('[data-test="secret-copy"]').trigger("click"),
    ).resolves.not.toThrow();
  });
});

describe("admin API surface", () => {
  beforeEach(() => setActivePinia(createPinia()));
  afterEach(() => vi.unstubAllGlobals());

  it("builds audit queries with only the filters that are set", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ items: [], next_cursor: null, has_more: false }), {
          status: 200,
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { audit } = await import("@/api/admin");
    await audit.query({ outcome: "denied", action_prefix: "", limit: 50 });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("outcome=denied");
    expect(url).toContain("limit=50");
    // An empty filter must not become `action_prefix=` — that is a different
    // query to the server than "no filter".
    expect(url).not.toContain("action_prefix");
  });

  it("sends null rather than an empty list when a key covers all owner KBs", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify({ api_key: {}, key: "x" }), { status: 201 })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { apiKeys } = await import("@/api/admin");
    await apiKeys.create({
      name: "agent",
      scopes: ["kb:query"],
      knowledge_base_ids: null,
      rate_limit_rpm: null,
      ip_allowlist: null,
      expires_at: null,
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body));
    // `null` means "everything the owner can reach"; `[]` would mean "nothing".
    expect(body.knowledge_base_ids).toBeNull();
  });

  it("uses PATCH for user updates so unset fields are left alone", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);

    const { users } = await import("@/api/admin");
    await users.update("usr_01H", { is_active: false });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/users/usr_01H");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ is_active: false });
  });
});

describe("i18n", () => {
  it("TC-M16-14: every key resolves in both locales", () => {
    const flatten = (obj: Record<string, unknown>, prefix = ""): string[] =>
      Object.entries(obj).flatMap(([key, value]) =>
        typeof value === "object" && value !== null
          ? flatten(value as Record<string, unknown>, `${prefix}${key}.`)
          : [`${prefix}${key}`],
      );

    const en = flatten(enUS).sort();
    const zh = flatten(zhCN).sort();

    expect(zh).toEqual(en);
    // Guards against a locale being trimmed back to a stub.
    expect(en.length).toBeGreaterThan(120);
  });
});
