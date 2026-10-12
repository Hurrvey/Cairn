import { flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import DashboardView from "@/features/dashboard/DashboardView.vue";
import KnowledgeView from "@/features/knowledge/KnowledgeView.vue";
import ModelsView from "@/features/models/ModelsView.vue";
import StorageView from "@/features/storage/StorageView.vue";

import { freshPinia, jsonResponse, makeRouter, mountWith, settle, signIn } from "./helpers";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

const KB = {
  id: "kb_test",
  name: "Engineering notes",
  slug: "engineering-notes",
  description: "Team handbook",
  icon_url: null,
  embedding_model_id: "mdl_1",
  embedding_dim: 384,
  metric: "cosine",
  vector_binding_id: "bnd_v",
  object_binding_id: "bnd_o",
  chunk_config: {},
  retrieval_config: {},
  active_index_version: 1,
  building_index_version: null,
  config_version: 1,
  owner_user_id: "usr_admin",
  status: "active",
  doc_count: 3,
  chunk_count: 12,
  bytes_used: 4096,
  last_indexed_at: "2026-09-20T10:00:00Z",
  created_at: "2026-09-01T00:00:00Z",
  reindex_required: false,
};

function mockFetch(overrides: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (path: string, _init?: RequestInit) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return jsonResponse(value);
    }
    if (path.startsWith("/v1/knowledge-bases?")) return jsonResponse({ items: [KB], next_cursor: null });
    if (path.startsWith("/v1/knowledge-bases/kb_test/documents/stats"))
      return jsonResponse({ counts: { indexed: 2, failed: 1 }, total: 3 });
    if (path === "/v1/knowledge-base-options")
      return jsonResponse({
        models: [],
        bindings: [],
        tokenizers: ["minilm"],
        supported_extensions: [".md"],
        max_upload_bytes: 1000,
      });
    if (path === "/v1/meta") return jsonResponse({ product: "Cairn", version: "0", api_version: "v1", role: "all", capabilities: { mcp: true } });
    if (path === "/v1/model-providers" || path === "/v1/models" || path === "/v1/storage-bindings") return jsonResponse([]);
    if (path === "/v1/mcp-service") return jsonResponse({ managed: false, state: "unknown" });
    if (path === "/v1/tasks/queues") return jsonResponse({ queues: [] });
    if (path.endsWith("/permissions"))
      return jsonResponse({ workspace_permissions: ["kb:create"], resource_permissions: { kb_test: ["kb:manage"] } });
    return jsonResponse([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("main product pages", () => {
  it("shows setup steps, attention items and the knowledge base list on the overview", async () => {
    const pinia = freshPinia();
    signIn("admin", ["platform:models", "platform:storage", "platform:settings"]);
    mockFetch();
    const wrapper = mountWith(DashboardView, { pinia, router: makeRouter() });
    await flushPromises();
    await settle();

    expect(wrapper.find('[data-test="setup-checklist"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="setup-model"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="setup-storage"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="attention-list"]').text()).toContain("1 failed documents in Engineering notes");
    expect(wrapper.find('[data-test="kb-row-kb_test"]').text()).toContain("Engineering notes");
    expect(wrapper.find('[data-test="retrieval-endpoint"]').text()).toContain("/v1/retrieval/query");
    wrapper.unmount();
  });

  it("renders the knowledge base list from the real response with a create entry", async () => {
    const pinia = freshPinia();
    signIn("admin", []);
    mockFetch();
    const wrapper = mountWith(KnowledgeView, { pinia, router: makeRouter() });
    await flushPromises();
    await settle();

    expect(wrapper.text()).toContain("Engineering notes");
    expect(wrapper.find('[data-test="create-kb"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="kb-row-kb_test"]').text()).toContain("Active");
    wrapper.unmount();
  });

  it("filters the knowledge base list by name without a request", async () => {
    const pinia = freshPinia();
    signIn("admin", []);
    const fetchMock = mockFetch();
    const wrapper = mountWith(KnowledgeView, { pinia, router: makeRouter() });
    await flushPromises();
    await settle();
    const calls = fetchMock.mock.calls.length;

    await wrapper.find('[data-test="kb-filter"]').setValue("nothing-here");
    expect(wrapper.text()).toContain("No knowledge base matches");
    expect(fetchMock.mock.calls.length).toBe(calls);
    wrapper.unmount();
  });

  it("offers local and S3-compatible document storage", async () => {
    const pinia = freshPinia();
    mockFetch();
    const wrapper = mountWith(StorageView, { pinia });
    await flushPromises();
    await settle();

    expect(wrapper.text()).toContain("Add one binding for document files and one for vectors");
    expect(wrapper.text()).not.toContain("S3");
    await wrapper.find('[data-test="add-storage"]').trigger("click");
    await settle();
    const sheet = document.body.querySelector('[data-test="storage-sheet"]');
    expect(sheet?.textContent).toContain("Local files in the persistent directory");
    expect(sheet?.textContent).toContain("Qdrant");
    expect(sheet?.textContent).toContain("S3-compatible");
    wrapper.unmount();
  });

  it("shows model setup and an actionable empty state", async () => {
    const pinia = freshPinia();
    mockFetch();
    const wrapper = mountWith(ModelsView, { pinia });
    await flushPromises();
    await settle();

    expect(wrapper.find('[data-test="add-provider"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("hosted vendors");
    expect(wrapper.find('[data-test="add-model"]').attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });

  it("sends S3 configuration and write-only credentials when creating storage", async () => {
    const pinia = freshPinia();
    const fetchMock = mockFetch({ "/v1/storage-bindings": [] });
    const wrapper = mountWith(StorageView, { pinia });
    await flushPromises();
    await wrapper.find('[data-test="add-storage"]').trigger("click");
    await settle();
    const field = (selector: string) => document.body.querySelector(selector) as HTMLInputElement;
    field('[data-test="storage-name"]').value = "Archive";
    field('[data-test="storage-name"]').dispatchEvent(new Event("input", { bubbles: true }));
    const driver = field('[data-test="storage-driver"]');
    driver.value = "s3";
    driver.dispatchEvent(new Event("change", { bubbles: true }));
    await settle();
    for (const [key, value] of [["bucket", "documents"], ["access-key", "access-id"], ["secret-key", "private-value"]]) {
      const input = field(`[data-test="storage-${key}"]`);
      input.value = value!;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await settle();
    field('[data-test="storage-submit"]').click();
    await flushPromises();
    const call = fetchMock.mock.calls.find((args) => args[0] === "/v1/storage-bindings" && (args[1] as RequestInit | undefined)?.method === "POST");
    expect(call).toBeDefined();
    const body = JSON.parse((call![1] as RequestInit).body as string);
    expect(body.driver).toBe("s3");
    expect(body.config.bucket).toBe("documents");
    expect(body.credentials.secret_access_key).toBe("private-value");
    expect(body.config.secret_access_key).toBeUndefined();
    wrapper.unmount();
  });

  it("does not present an unhealthy storage probe as success", async () => {
    const pinia = freshPinia();
    mockFetch({
      "/v1/storage-bindings/binding/test": { healthy: false },
      "/v1/storage-bindings": [
        { id: "binding", name: "Offline", kind: "object", driver: "local", config: {}, health_state: "unavailable" },
      ],
    });
    const wrapper = mountWith(StorageView, { pinia });
    await flushPromises();
    await settle();

    await wrapper.find('[data-test="test-binding"]').trigger("click");
    await flushPromises();
    await settle();

    const result = wrapper.find('[data-test="binding-test-result"]');
    expect(result.text()).toContain("failed");
    expect(result.classes()).toContain("text-bad");
    expect(wrapper.find('[data-test="binding-health"]').text()).toContain("Unavailable");
    wrapper.unmount();
  });
});
