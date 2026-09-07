/**
 * API client, router guard, and i18n completeness.
 * TC-M16-02, TC-M16-03, TC-M16-14.
 */

import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError, onApiError, request } from "@/api/client";
import enUS from "@/locales/en-US.json";
import zhCN from "@/locales/zh-CN.json";
import { useSessionStore } from "@/stores/session";

function problem(status: number, code: string, detail = "nope"): Response {
  return new Response(JSON.stringify({ code, detail, status, request_id: "req_test" }), {
    status,
    headers: { "Content-Type": "application/problem+json" },
  });
}

describe("api client", () => {
  beforeEach(() => setActivePinia(createPinia()));
  afterEach(() => vi.unstubAllGlobals());

  it("parses problem+json into a typed error carrying the stable code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(problem(404, "RESOURCE_NOT_FOUND", "Gone."))),
    );

    await expect(request("/v1/thing")).rejects.toBeInstanceOf(ApiError);
    try {
      await request("/v1/thing");
    } catch (error) {
      const api = error as ApiError;
      expect(api.code).toBe("RESOURCE_NOT_FOUND");
      expect(api.status).toBe(404);
      expect(api.requestId).toBe("req_test");
    }
  });

  it("groups field errors by field for inline form display", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "PASSWORD_POLICY_VIOLATION",
            detail: "The password does not meet the policy.",
            status: 400,
            errors: [
              { field: "new_password", code: "TOO_SHORT", detail: "must be at least 12" },
              { field: "new_password", code: "COMMON_PASSWORD", detail: "too common" },
            ],
          }),
          { status: 400 },
        ),
      ),
    );

    try {
      await request("/v1/auth/complete-initial-setup", { method: "POST", body: {} });
    } catch (error) {
      expect((error as ApiError).byField()["new_password"]).toHaveLength(2);
    }
  });

  it("distinguishes a transport failure from an API error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("connection refused")));
    await expect(request("/v1/thing")).rejects.toBeInstanceOf(NetworkError);
  });

  it("attaches the CSRF token to unsafe cookie-authenticated requests", async () => {
    document.cookie = "cairn_csrf=csrf-value-123";
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await request("/v1/auth/logout", { method: "POST" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-value-123");
  });

  it("omits CSRF on safe methods and on bearer-authenticated requests", async () => {
    document.cookie = "cairn_csrf=csrf-value-123";
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(new Response("{}", { status: 200 })),
    );
    vi.stubGlobal("fetch", fetchMock);

    await request("/v1/me");
    await request("/v1/auth/complete-initial-setup", { method: "POST", token: "chg_x", body: {} });

    for (const [, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
    }
  });

  it("TC-M16-03: PASSWORD_CHANGE_REQUIRED from ANY endpoint reopens the dialog", async () => {
    const session = useSessionStore();
    expect(session.mustChangePassword).toBe(false);

    const unsubscribe = onApiError("PASSWORD_CHANGE_REQUIRED", () => {
      session.requireCredentialChange();
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() =>
        Promise.resolve(problem(403, "PASSWORD_CHANGE_REQUIRED", "Change required.")),
      ),
    );

    // Deliberately an unrelated endpoint: the guarantee is global, not something
    // the auth screens opt into.
    await expect(request("/v1/knowledge-bases")).rejects.toBeInstanceOf(ApiError);
    expect(session.mustChangePassword).toBe(true);

    unsubscribe();
  });
});

describe("session store", () => {
  beforeEach(() => setActivePinia(createPinia()));
  afterEach(() => vi.unstubAllGlobals());

  it("TC-M16-02: enters credentialChange and holds no session token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "password_change_required",
            change_token: "chg_abc",
            reason: "initial_admin_setup",
            expires_in: 600,
            policy: { min_length: 12, max_length: 256, require_classes: 3, classes: [], username_editable: true, disallow_previous: true },
            user: { id: "usr_1", username: "admin", email: null, display_name: null, role: "admin", must_change_password: true, is_active: true, last_login_at: null, created_at: "2026-08-31T00:00:00Z" },
          }),
          { status: 200 },
        ),
      ),
    );

    const session = useSessionStore();
    const state = await session.signIn("admin", "initial");

    expect(state).toBe("credentialChange");
    expect(session.isAuthenticated).toBe(false);
    expect(session.changeToken).toBe("chg_abc");
  });

  it("keeps the change token out of persistent storage", async () => {
    // Persisting it would survive a reload — but the requirement is durable
    // server-side, so a reload should re-authenticate and mint a fresh token.
    const session = useSessionStore();
    session.changeToken = "chg_secret";
    expect(localStorage.getItem("cairn.changeToken")).toBeNull();
    expect(JSON.stringify(localStorage)).not.toContain("chg_secret");
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
    expect(en.length).toBeGreaterThan(25);
  });
});
