/**
 * Forced credential dialog — TC-M16-01 … TC-M16-05.
 *
 * These verify the UI half of FR-P-02. The server-side half is covered by the
 * Python integration suite; neither is sufficient alone, because a user who can
 * dismiss the dialog is left staring at an application where every request 403s.
 */

import ElementPlus from "element-plus";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import { createI18n } from "vue-i18n";

import enUS from "@/locales/en-US.json";
import zhCN from "@/locales/zh-CN.json";
import ForcedCredentialDialog from "@/features/auth/ForcedCredentialDialog.vue";
import { useSessionStore } from "@/stores/session";

const POLICY = {
  min_length: 12,
  max_length: 256,
  require_classes: 3,
  classes: ["lowercase", "uppercase", "digit", "symbol"],
  username_editable: true,
  disallow_previous: true,
};

const i18n = createI18n({
  legacy: false,
  locale: "en-US",
  fallbackLocale: "en-US",
  messages: { "en-US": enUS, "zh-CN": zhCN },
});

function mountDialog(): VueWrapper {
  return mount(ForcedCredentialDialog, {
    global: { plugins: [i18n, ElementPlus] },
    attachTo: document.body,
  });
}

function flagged() {
  const session = useSessionStore();
  session.mustChangePassword = true;
  session.changeToken = "chg_test_token";
  session.changeReason = "initial_admin_setup";
  session.policy = POLICY;
  session.user = {
    id: "usr_01H",
    username: "admin",
    email: null,
    display_name: "Administrator",
    role: "admin",
    must_change_password: true,
    is_active: true,
    last_login_at: null,
    created_at: "2026-08-31T00:00:00Z",
  };
  return session;
}

const q = (selector: string) => document.body.querySelector(selector);

/** el-dialog teleports to <body>; drive the real input and let v-model react.
 *  ElInput sets inheritAttrs:false and forwards attrs to the inner <input>, so
 *  data-test may land on the input itself rather than on a wrapper. */
async function setField(name: string, value: string): Promise<void> {
  const target = q(`[data-test="${name}"]`);
  const input = (
    target instanceof HTMLInputElement ? target : target?.querySelector("input")
  ) as HTMLInputElement | null;
  if (!input) throw new Error(`field ${name} is not rendered`);
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await nextTick();
}

describe("ForcedCredentialDialog", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  });

  it("is hidden when no credential change is outstanding", async () => {
    const wrapper = mountDialog();
    await wrapper.vm.$nextTick();
    expect(q('[data-test="forced-credential-dialog"]')).toBeNull();
  });

  it("TC-M16-01: offers no close affordance and ignores escape and backdrop", async () => {
    flagged();
    const wrapper = mountDialog();
    await wrapper.vm.$nextTick();

    const dialog = q('[data-test="forced-credential-dialog"]');
    expect(dialog).not.toBeNull();

    // No ✕ button is rendered at all — not merely disabled.
    expect(q(".el-dialog__headerbtn")).toBeNull();

    // Element Plus reads these props to decide whether ESC and backdrop clicks
    // close the dialog. Asserting the props is the honest check: simulating a
    // keypress would test the library, not our configuration.
    const props = wrapper.findComponent({ name: "ElDialog" }).props();
    expect(props.closeOnPressEscape).toBe(false);
    expect(props.closeOnClickModal).toBe(false);
    expect(props.showClose).toBe(false);
  });

  it("TC-M16-04: renders policy rules from the server, not from hardcoded values", async () => {
    const session = flagged();
    // A deployment that raised min_length must see the new rule with no
    // frontend release. Hardcoding 12 here would silently lie to that operator.
    session.policy = { ...POLICY, min_length: 20, require_classes: 4 };

    const wrapper = mountDialog();
    await wrapper.vm.$nextTick();

    const rules = q('[data-test="policy-rules"]');
    expect(rules?.textContent).toContain("At least 20 characters");
    expect(rules?.textContent).toContain("At least 4 of");
  });

  it("TC-M16-04: rule indicators track input live", async () => {
    flagged();
    const wrapper = mountDialog();
    await wrapper.vm.$nextTick();

    const lengthRule = () => q('[data-rule="length"]');
    expect(lengthRule()?.classList.contains("ok")).toBe(false);

    await setField("new-password", "Correct-Horse-Battery-9");
    await wrapper.vm.$nextTick();

    expect(lengthRule()?.classList.contains("ok")).toBe(true);
    expect(q('[data-rule="classes"]')?.classList.contains("ok")).toBe(true);
  });

  it("blocks submission until every rule passes", async () => {
    flagged();
    const wrapper = mountDialog();
    await wrapper.vm.$nextTick();

    const submit = () => q('[data-test="submit"]') as HTMLButtonElement | null;
    expect(submit()?.disabled).toBe(true);

    await setField("current-password", "initial-password");
    await setField("new-password", "Correct-Horse-Battery-9");
    await setField("confirm-password", "Correct-Horse-Battery-9");
    await wrapper.vm.$nextTick();

    expect(submit()?.disabled).toBe(false);
  });

  it("TC-M16-05: submits the username and password as a single request", async () => {
    flagged();
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            status: "ok",
            user: { ...flagged().user, username: "dana.ops", must_change_password: false },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const wrapper = mountDialog();
    await wrapper.vm.$nextTick();

    await setField("username", "dana.ops");
    await setField("current-password", "initial-password");
    await setField("new-password", "Correct-Horse-Battery-9");
    await setField("confirm-password", "Correct-Horse-Battery-9");
    await wrapper.vm.$nextTick();

    await (q('[data-test="submit"]') as HTMLButtonElement).click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    // One call, carrying both changes — not a rename followed by a password change.
    const setupCalls = fetchMock.mock.calls.filter(
      ([url]) => String(url) === "/v1/auth/complete-initial-setup",
    );
    expect(setupCalls).toHaveLength(1);

    const [, init] = setupCalls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body));
    expect(body).toMatchObject({
      current_password: "initial-password",
      new_password: "Correct-Horse-Battery-9",
      new_username: "dana.ops",
    });
    // The change token travels as a bearer header, never as a cookie.
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(
      "Bearer chg_test_token",
    );
  });

  it("surfaces a taken username against the username field only", async () => {
    flagged();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "USERNAME_TAKEN",
            detail: "That username is already in use.",
            status: 409,
            request_id: "req_x",
          }),
          { status: 409, headers: { "Content-Type": "application/problem+json" } },
        ),
      ),
    );

    const wrapper = mountDialog();
    await wrapper.vm.$nextTick();

    await setField("username", "taken.name");
    await setField("current-password", "initial-password");
    await setField("new-password", "Correct-Horse-Battery-9");
    await setField("confirm-password", "Correct-Horse-Battery-9");
    await wrapper.vm.$nextTick();

    await (q('[data-test="submit"]') as HTMLButtonElement).click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(q('[data-test="form-error"]')?.textContent).toContain("already in use");
    // The server rolled the whole change back, so the dialog must stay open.
    expect(useSessionStore().mustChangePassword).toBe(true);
  });
});
