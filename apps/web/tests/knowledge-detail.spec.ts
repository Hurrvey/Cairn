/**
 * Knowledge base detail: master–detail documents, search console, and the
 * chunk editor. Uses the real components against a mocked API.
 */
import { flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import KnowledgeDetailView from "@/features/knowledge/KnowledgeDetailView.vue";
import { buildQuery } from "@/api/knowledge";
import { useToasts } from "@/shared/composables/useToasts";

import { freshPinia, jsonResponse, makeRouter, mountWith, settle, signIn } from "./helpers";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

const KB = {
  id: "kb_test",
  name: "Field notes",
  description: "Everything about the field",
  status: "active",
  doc_count: 2,
  chunk_count: 4,
  bytes_used: 2048,
  embedding_dim: 384,
  metric: "cosine",
  active_index_version: 2,
  building_index_version: null,
  last_indexed_at: "2026-09-20T00:00:00Z",
  embedding_model_id: "mdl",
  retrieval_config: { search_mode: "hybrid", top_k: 5, expand_parent: false },
  chunk_config: { strategy: "parent_child" },
  reindex_required: false,
};

const DOCS = [
  { id: "doc_ok", title: "Saturn.md", state: "indexed", progress_pct: 100, revision: 1, chunk_count: 2, mime_type: "text/markdown", size_bytes: 120, page_count: null, error_code: null, error_detail: null, stage_detail: null, created_at: "2026-09-20T00:00:00Z", indexed_at: "2026-09-20T00:01:00Z" },
  { id: "doc_bad", title: "Broken.pdf", state: "failed", progress_pct: 30, revision: 1, chunk_count: 0, mime_type: "application/pdf", size_bytes: 9000, page_count: 3, error_code: "PDF_ENCRYPTED", error_detail: "The PDF is password protected.", stage_detail: null, created_at: "2026-09-20T00:00:00Z", indexed_at: null },
];

function mockApi(extra: (path: string, init?: RequestInit) => Response | undefined = () => undefined) {
  const chunks = [
    { id: "chk_1", document_id: "doc_ok", index_version: 2, parent_id: null, ordinal: 0, content: "Saturn has rings.", token_count: 4, metadata: { embed: true, page: 1 }, is_edited: false },
    { id: "chk_p", document_id: "doc_ok", index_version: 2, parent_id: null, ordinal: 1, content: "Parent context", token_count: 2, metadata: { embed: false }, is_edited: false },
  ];
  const fetchMock = vi.fn(async (path: string, init?: RequestInit) => {
    const custom = extra(path, init);
    if (custom) return custom;
    if (path.includes("/documents?")) return jsonResponse({ items: DOCS, next_cursor: null });
    if (path.endsWith("/documents/stats")) return jsonResponse({ counts: { indexed: 1, failed: 1 }, total: 2 });
    if (path.includes("/documents/doc_ok/chunks")) return jsonResponse(chunks);
    if (path.endsWith("options")) return jsonResponse({ supported_extensions: [".md", ".pdf"], max_upload_bytes: 1024 * 1024, models: [{ id: "mdl", display_name: "MiniLM", dimension: 384, model_key: "minilm", max_input_tokens: 256, provider_id: "p", tokenizer_id: "minilm" }], bindings: [], tokenizers: [] });
    if (path.endsWith("/permissions")) return jsonResponse({ workspace_permissions: [], resource_permissions: { kb_test: ["kb:manage"] } });
    if (path === "/v1/meta") return jsonResponse({ capabilities: { rerank: false } });
    if (path === "/v1/retrieval/query")
      return jsonResponse({
        request_id: "req_1",
        results: [{ chunk_id: "chk_1", document_id: "doc_ok", knowledge_base_id: "kb_test", content: "Saturn has rings.", score: 0.91, metadata: { page: 1 } }],
        usage: { embedding_tokens: 3, latency_ms: { total: 12 } },
        degraded: [{ stage: "rerank", reason: "unsupported", detail: "Rerank is not configured." }],
        partial_failures: [],
      });
    if (path.startsWith("/v1/knowledge-bases/kb_test/chunks/") && init?.method === "PATCH") {
      const updated = { ...chunks[0]!, content: JSON.parse(String(init.body)).content, token_count: 5, is_edited: true };
      chunks[0] = updated;
      return jsonResponse(updated);
    }
    return jsonResponse(KB);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function mountDetail(path = "/knowledge/kb_test") {
  const pinia = freshPinia();
  signIn("user", []);
  const router = makeRouter([
    { path: "/knowledge", name: "knowledge", component: { template: "<div />" } },
    { path: "/knowledge/:id", name: "knowledge-detail", component: KnowledgeDetailView },
  ]);
  await router.push(path);
  const wrapper = mountWith(KnowledgeDetailView, { pinia, router });
  await flushPromises();
  await settle();
  return { wrapper, router };
}

describe("KnowledgeDetailView", () => {
  it("lists documents with pipeline state and opens a failed document with its error", async () => {
    mockApi();
    const { wrapper, router } = await mountDetail();

    expect(wrapper.find('[data-test="page-title"]').text()).toBe("Field notes");
    expect(wrapper.find('[data-test="kb-status"]').text()).toContain("Active");
    expect(wrapper.findAll('[data-test="document-list"] li')).toHaveLength(2);
    expect(wrapper.find('[data-test="no-selection"]').exists()).toBe(true);

    await wrapper.find('[data-test="doc-row-doc_bad"]').trigger("click");
    await flushPromises();
    await settle();
    expect(router.currentRoute.value.query.doc).toBe("doc_bad");
    expect(wrapper.find('[data-test="doc-error"]').text()).toContain("PDF_ENCRYPTED");
    expect(wrapper.find('[data-test="doc-error"]').text()).toContain("password protected");
    expect(wrapper.find('[data-test="retry-doc"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("filters documents by state from the URL so links to failures land on them", async () => {
    mockApi();
    const { wrapper } = await mountDetail("/knowledge/kb_test?state=failed");
    const rows = wrapper.findAll('[data-test="document-list"] li');
    expect(rows).toHaveLength(1);
    expect(rows[0]!.text()).toContain("Broken.pdf");
    wrapper.unmount();
  });

  it("shows chunks for an indexed document and saves an in-place edit", async () => {
    const fetchMock = mockApi();
    const { wrapper } = await mountDetail("/knowledge/kb_test?doc=doc_ok");

    expect(wrapper.find('[data-test="chunk-list"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="chunk-chk_1"]').text()).toContain("Saturn has rings.");
    expect(wrapper.find('[data-test="chunk-chk_p"]').text()).toContain("Parent context");

    await wrapper.find('[data-test="chunk-chk_1"] [data-test="edit-chunk"]').trigger("click");
    const editor = wrapper.find('[data-test="chunk-editor"]');
    await editor.setValue("Saturn has many rings.");
    await wrapper.find('[data-test="save-chunk"]').trigger("click");
    await flushPromises();
    await settle();

    const patch = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PATCH");
    expect(patch?.[0]).toBe("/v1/knowledge-bases/kb_test/chunks/chk_1");
    expect(wrapper.find('[data-test="chunk-chk_1"]').text()).toContain("Saturn has many rings.");
    expect(wrapper.find('[data-test="chunk-chk_1"]').text()).toContain("Edited");
    expect(useToasts().items.some((toast) => toast.kind === "success")).toBe(true);
    wrapper.unmount();
  });

  it("runs a search, shows degradation notices and links hits back to documents", async () => {
    const fetchMock = mockApi();
    const { wrapper, router } = await mountDetail("/knowledge/kb_test?tab=search");

    const input = wrapper.find('[data-test="retrieval-query"]');
    await input.setValue("Which planet has rings?");
    await wrapper.find('[data-test="run-search"]').trigger("click");
    await flushPromises();
    await settle();

    const query = fetchMock.mock.calls.find(([path]) => path === "/v1/retrieval/query");
    const body = JSON.parse(String((query?.[1] as RequestInit).body));
    expect(body.targets[0].knowledge_base_id).toBe("kb_test");
    expect(body.search_mode).toBe("hybrid");
    expect(body.rerank.enabled).toBe(false);

    expect(wrapper.find('[data-test="search-results"]').text()).toContain("Saturn has rings.");
    expect(wrapper.find('[data-test="search-notice"]').text()).toContain("Rerank is not configured.");
    expect(wrapper.find('[data-test="search-summary"]').text()).toContain("1 passages");
    expect(router.currentRoute.value.query.q).toBe("Which planet has rings?");

    await wrapper.find('[data-test="search-results"] button').trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.query.doc).toBe("doc_ok");
    expect(router.currentRoute.value.query.tab).toBeUndefined();
    wrapper.unmount();
  });

  it("explains restricted content access and offers temporary access", async () => {
    mockApi((path) =>
      path.includes("/documents?")
        ? new Response(JSON.stringify({ code: "CONTENT_ACCESS_REQUIRES_GRANT", detail: "Open break-glass access.", status: 403 }), { status: 403 })
        : undefined,
    );
    const { wrapper } = await mountDetail();
    expect(wrapper.find('[data-test="access-blocked"]').exists()).toBe(true);
    await wrapper.find('[data-test="request-access"]').trigger("click");
    await settle();
    expect(document.body.querySelector('[data-test="break-glass-dialog"]')).not.toBeNull();
    wrapper.unmount();
  });
});

describe("buildQuery", () => {
  it("only sends weights for weighted hybrid fusion and never a rerank model when disabled", () => {
    const base = { mode: "hybrid" as const, topK: 5, fusion: "rrf" as const, dense: 0.7, sparse: 0.3, expandParent: true, rerank: false, rerankModel: "x", rerankTopN: 5 };
    const rrf = buildQuery("kb", "q", base);
    expect(rrf.fusion?.method).toBe("rrf");
    expect(rrf.weights).toBeUndefined();
    expect(rrf.rerank?.enabled).toBe(false);
    expect(rrf.options?.expand_parent).toBe(true);

    const weighted = buildQuery("kb", "q", { ...base, fusion: "weighted", rerank: true });
    expect(weighted.weights).toEqual({ dense: 0.7, sparse: 0.3 });
    expect(weighted.rerank).toMatchObject({ enabled: true, model_id: "x", top_n: 5 });

    const vector = buildQuery("kb", "q", { ...base, mode: "vector" });
    expect(vector.fusion).toBeUndefined();
  });
});
