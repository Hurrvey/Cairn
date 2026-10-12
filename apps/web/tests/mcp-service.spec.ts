import { flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MCPConfig, MCPServiceSnapshot } from "@/api/mcp";
import { router as applicationRouter } from "@/app/router";
import AppShell from "@/app/AppShell.vue";
import MCPServiceView from "@/features/mcp/MCPServiceView.vue";
import { useSessionStore } from "@/stores/session";

import { freshPinia, jsonResponse, makeRouter, mountWith, settle, signIn } from "./helpers";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

const config: MCPConfig = {
  port: 8081,
  auto_start: true,
  allowed_hosts: ["localhost"],
  allowed_origins: ["https://cairn.example"],
  request_timeout_s: 15,
  max_request_bytes: 65_536,
};

function serviceSnapshot(overrides: Partial<MCPServiceSnapshot> = {}): MCPServiceSnapshot {
  return {
    config,
    generation: 4,
    observed_generation: 4,
    desired_state: "running",
    state: "running",
    effective_port: 8084,
    started_at: "2026-09-17T08:00:00.000Z",
    heartbeat_at: new Date().toISOString(),
    last_error: null,
    allowed_ports: [8081, 8084, 8090],
    managed: true,
    ...overrides,
  };
}

function mountView() {
  return mountWith(MCPServiceView, { pinia: freshPinia() });
}

function mockService(current: MCPServiceSnapshot, logs: unknown[] = []): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (path: string) => {
    if (path.startsWith("/v1/mcp-service/logs")) return jsonResponse({ items: logs });
    return jsonResponse(current);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("MCP service API", () => {
  it("wraps config updates with the expected generation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ generation: 8 }));
    vi.stubGlobal("fetch", fetchMock);
    const { mcpService } = await import("@/api/mcp");

    await mcpService.updateConfig(7, config);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/mcp-service/config");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ expected_generation: 7, config });
  });

  it("posts service actions with the expected generation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ generation: 10 }));
    vi.stubGlobal("fetch", fetchMock);
    const { mcpService } = await import("@/api/mcp");

    await mcpService.act(9, "restart");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/mcp-service/actions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ expected_generation: 9, action: "restart" });
  });

  it("encodes supported log filters for reads and downloads", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);
    const { mcpService } = await import("@/api/mcp");
    const filters = { after_id: 42, limit: 100, level: "WARNING" as const, since: "2026-09-17T09:30:00.000Z" };

    await mcpService.logs(filters);

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/v1/mcp-service/logs?after_id=42&limit=100&level=WARNING&since=2026-09-17T09%3A30%3A00.000Z");
    expect(mcpService.logsDownloadUrl(filters)).toBe(
      "/v1/mcp-service/logs/download?after_id=42&limit=100&level=WARNING&since=2026-09-17T09%3A30%3A00.000Z",
    );
  });
});

describe("MCP service view", () => {
  it("shows desired and observed state plus both running endpoints", async () => {
    mockService(serviceSnapshot());

    const wrapper = mountView();
    await flushPromises();
    await settle();

    expect(wrapper.get('[data-test="actual-state"]').text()).toContain("Running");
    expect(wrapper.get('[data-test="desired-state"]').text()).toContain("Running");
    expect(wrapper.get('[data-test="generation"]').text()).toContain("4 / 4");
    expect(wrapper.get('[data-test="compat-endpoint"]').text()).toContain(`${window.location.origin}/mcp`);
    expect(wrapper.get('[data-test="direct-endpoint"]').text()).toContain("http://localhost:8084/mcp");
    expect(
      wrapper
        .findAll('[data-test="config-port"] option')
        .map((option) => Number((option.element as HTMLOptionElement).value)),
    ).toEqual([8081, 8084, 8090]);
    wrapper.unmount();
  });

  it("reports a stale heartbeat as unknown and hides unusable endpoints", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-17T10:00:20.000Z"));
    mockService(serviceSnapshot({ heartbeat_at: "2026-09-17T10:00:00.000Z" }));

    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.get('[data-test="actual-state"]').text()).toContain("Unknown");
    expect(wrapper.find('[data-test="direct-endpoint"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("healthy");
    wrapper.unmount();
  });

  it("explains unmanaged deployments and disables service actions", async () => {
    mockService(serviceSnapshot({ managed: false, state: "unknown", effective_port: null, heartbeat_at: null }));

    const wrapper = mountView();
    await flushPromises();
    await settle();

    expect(wrapper.get('[data-test="unmanaged-notice"]').text()).toContain("does not manage");
    for (const action of ["start", "stop", "restart"]) {
      expect(wrapper.get(`[data-test="action-${action}"]`).attributes("disabled")).toBeDefined();
    }
    wrapper.unmount();
  });

  it("sends actions with the currently displayed generation", async () => {
    const fetchMock = mockService(serviceSnapshot());
    const wrapper = mountView();
    await flushPromises();
    await settle();

    await wrapper.get('[data-test="action-restart"]').trigger("click");
    await flushPromises();

    const actionCall = fetchMock.mock.calls.find(([path]) => path === "/v1/mcp-service/actions") as
      | [string, RequestInit]
      | undefined;
    expect(JSON.parse(String(actionCall?.[1].body))).toEqual({ expected_generation: 4, action: "restart" });
    wrapper.unmount();
  });

  it("renders safe log details as text and configures the download anchor", async () => {
    mockService(serviceSnapshot(), [
      {
        id: 43,
        at: "2026-09-17T09:30:00.000Z",
        level: "WARNING",
        event: "request.rejected",
        detail: '<img src=x onerror="alert(1)"> rejected safely',
        request_id: "req_43",
        status: 403,
        duration_ms: 7,
      },
    ]);

    const wrapper = mountView();
    await flushPromises();
    await settle();

    expect(wrapper.get('[data-test="log-detail"]').text()).toContain('<img src=x onerror="alert(1)"> rejected safely');
    expect(wrapper.find('[data-test="log-detail"] img').exists()).toBe(false);
    const download = wrapper.get('[data-test="download-logs"]');
    expect(download.attributes("href")).toBe("/v1/mcp-service/logs/download?limit=100");
    expect(download.attributes("download")).toBeDefined();
    wrapper.unmount();
  });

  it("waits for each poll to finish and aborts outstanding requests on cleanup", async () => {
    vi.useFakeTimers();
    const current = serviceSnapshot();
    let snapshotCalls = 0;
    let releaseSecondSnapshot: (() => void) | undefined;
    const secondSnapshot = new Promise<Response>((resolve) => {
      releaseSecondSnapshot = () => resolve(jsonResponse(current));
    });
    const signals: AbortSignal[] = [];
    const fetchMock = vi.fn((path: string, init: RequestInit) => {
      if (init.signal) signals.push(init.signal);
      if (path === "/v1/mcp-service") {
        snapshotCalls += 1;
        if (snapshotCalls === 2) return secondSnapshot;
        return Promise.resolve(jsonResponse(current));
      }
      return Promise.resolve(jsonResponse({ items: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const wrapper = mountView();
    await flushPromises();
    expect(snapshotCalls).toBe(1);

    await vi.advanceTimersByTimeAsync(2_000);
    expect(snapshotCalls).toBe(2);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(snapshotCalls).toBe(2);

    wrapper.unmount();
    expect(signals.some((signal) => signal.aborted)).toBe(true);
    releaseSecondSnapshot?.();
  });

  it("keeps polling service state without following logs when live follow is off", async () => {
    vi.useFakeTimers();
    const current = serviceSnapshot();
    const fetchMock = vi.fn((path: string) =>
      Promise.resolve(jsonResponse(path.startsWith("/v1/mcp-service/logs") ? { items: [] } : current)),
    );
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mountView();
    await flushPromises();

    const switches = wrapper.findAll('[role="switch"]');
    await switches.at(-1)!.trigger("click");
    await vi.advanceTimersByTimeAsync(2_000);

    expect(fetchMock.mock.calls.filter(([path]) => path === "/v1/mcp-service")).toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([path]) => String(path).startsWith("/v1/mcp-service/logs?"))).toHaveLength(1);
    wrapper.unmount();
  });
});

describe("MCP service navigation", () => {
  it("registers the exact admin route", () => {
    const route = applicationRouter.getRoutes().find(({ name }) => name === "mcp-service");
    expect(route?.path).toBe("/mcp-service/nav");
    expect(route?.meta.capability).toBe("platform:settings");
  });

  it("shows the service link only with platform settings capability", async () => {
    const pinia = freshPinia();
    const router = makeRouter();
    await router.push("/");
    const session = signIn("admin", []);
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ items: [] })));
    const wrapper = mountWith(AppShell, {
      pinia,
      router,
      global: {
        stubs: {
          RouterView: { template: "<div />" },
          ForcedCredentialDialog: { template: "<div />" },
        },
      },
    });

    expect(wrapper.find('[data-test="nav-mcp-service"]').exists()).toBe(false);
    session.permissions = ["platform:settings"];
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-test="nav-mcp-service"]').text()).toContain("MCP service");
    expect(useSessionStore().permissions).toContain("platform:settings");
    wrapper.unmount();
  });
});
