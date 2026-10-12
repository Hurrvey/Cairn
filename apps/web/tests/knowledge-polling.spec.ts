import { flushPromises } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";

import KnowledgeDetailView from "@/features/knowledge/KnowledgeDetailView.vue";

import { freshPinia, jsonResponse, makeRouter, mountWith, signIn } from "./helpers";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

const KB = {
  id: "kb_test",
  name: "Notes",
  status: "active",
  doc_count: 1,
  chunk_count: 0,
  bytes_used: 0,
  embedding_dim: 384,
  metric: "cosine",
  active_index_version: 1,
  building_index_version: null,
  last_indexed_at: null,
  embedding_model_id: "mdl",
  retrieval_config: {},
  chunk_config: {},
};

it("keeps polling while a document is still processing and stops on unmount", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn(async (path: string) => {
    if (path.includes("/documents?"))
      return jsonResponse({
        items: [
          {
            id: "doc_test",
            title: "Pending document",
            state: "embedding",
            progress_pct: 50,
            created_at: "2026-09-20T00:00:00Z",
            indexed_at: null,
          },
        ],
        next_cursor: "more",
      });
    if (path.endsWith("/documents/stats")) return jsonResponse({ counts: { embedding: 1 }, total: 1 });
    if (path.endsWith("options")) return jsonResponse({ supported_extensions: [".md"], max_upload_bytes: 1024, models: [], bindings: [], tokenizers: [] });
    if (path.endsWith("/permissions")) return jsonResponse({ workspace_permissions: [], resource_permissions: { kb_test: ["kb:write"] } });
    if (path === "/v1/meta") return jsonResponse({ capabilities: {} });
    return jsonResponse(KB);
  });
  vi.stubGlobal("fetch", fetchMock);
  const pinia = freshPinia();
  signIn("user", []);
  const router = makeRouter([{ path: "/knowledge/:id", name: "knowledge-detail", component: KnowledgeDetailView }]);
  await router.push("/knowledge/kb_test");
  const wrapper = mountWith(KnowledgeDetailView, { pinia, router });
  await flushPromises();
  await vi.advanceTimersByTimeAsync(3100);

  const documentCalls = () => fetchMock.mock.calls.filter(([path]) => String(path).includes("/documents?"));
  expect(documentCalls().length).toBeGreaterThanOrEqual(2);
  expect(wrapper.find('[data-test="doc-row-doc_test"]').attributes("data-state")).toBe("embedding");

  wrapper.unmount();
  const calls = fetchMock.mock.calls.length;
  await vi.advanceTimersByTimeAsync(20000);
  expect(fetchMock).toHaveBeenCalledTimes(calls);
});

it("slows polling down once every document is settled", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn(async (path: string) => {
    if (path.includes("/documents?"))
      return jsonResponse({
        items: [{ id: "doc_done", title: "Done", state: "indexed", progress_pct: 100, created_at: "2026-09-20T00:00:00Z", indexed_at: "2026-09-20T00:01:00Z" }],
        next_cursor: null,
      });
    if (path.endsWith("/documents/stats")) return jsonResponse({ counts: { indexed: 1 }, total: 1 });
    if (path.endsWith("options")) return jsonResponse({ supported_extensions: [".md"], max_upload_bytes: 1024, models: [], bindings: [], tokenizers: [] });
    if (path.endsWith("/permissions")) return jsonResponse({ workspace_permissions: [], resource_permissions: {} });
    if (path === "/v1/meta") return jsonResponse({ capabilities: {} });
    return jsonResponse(KB);
  });
  vi.stubGlobal("fetch", fetchMock);
  const pinia = freshPinia();
  signIn("user", []);
  const router = makeRouter([{ path: "/knowledge/:id", name: "knowledge-detail", component: KnowledgeDetailView }]);
  await router.push("/knowledge/kb_test");
  const wrapper = mountWith(KnowledgeDetailView, { pinia, router });
  await flushPromises();
  const documentCalls = () => fetchMock.mock.calls.filter(([path]) => String(path).includes("/documents?")).length;
  const initial = documentCalls();

  await vi.advanceTimersByTimeAsync(3100);
  expect(documentCalls()).toBe(initial);
  await vi.advanceTimersByTimeAsync(9500);
  expect(documentCalls()).toBe(initial + 1);
  wrapper.unmount();
});
