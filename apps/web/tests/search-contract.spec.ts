/**
 * Search console contract options: filters, ranking toggles, highlights, the
 * explain panel and copy-as-curl, against a mocked API.
 */
import { flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import { buildFilters, buildQuery, curlFor, FilterSyntaxError, type QueryOptions } from "@/api/knowledge";
import KnowledgeDetailView from "@/features/knowledge/KnowledgeDetailView.vue";

import { freshPinia, jsonResponse, makeRouter, mountWith, settle, signIn } from "./helpers";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

const BASE: QueryOptions = {
  mode: "hybrid",
  topK: 5,
  fusion: "rrf",
  dense: 0.7,
  sparse: 0.3,
  expandParent: false,
  rerank: false,
  rerankModel: "",
  rerankTopN: 5,
};

describe("request building", () => {
  it("sends only the filters that are set, with dates as UTC instants", () => {
    expect(buildFilters(BASE)).toBeNull();
    const filters = buildFilters({
      ...BASE,
      metadataFilter: '{"lang": "en", "tags": {"$in": ["ops"]}}',
      documentIds: ["doc_a"],
      createdAfter: "2026-01-02T03:04",
    });
    expect(filters?.metadata).toEqual({ lang: "en", tags: { $in: ["ops"] } });
    expect(filters?.document_ids).toEqual(["doc_a"]);
    expect(filters?.created_after).toBe(new Date("2026-01-02T03:04").toISOString());
    expect(filters?.created_before).toBeUndefined();
  });

  it("refuses a metadata filter that is not a JSON object", () => {
    expect(() => buildFilters({ ...BASE, metadataFilter: "[1, 2]" })).toThrow(FilterSyntaxError);
    expect(() => buildFilters({ ...BASE, metadataFilter: "{lang: en}" })).toThrow(FilterSyntaxError);
  });

  it("maps ranking toggles onto the request options", () => {
    const body = buildQuery("kb", "q", { ...BASE, dedupe: "by_document", mmr: true, mmrLambda: 0.3, highlights: true, explain: true });
    expect(body.options).toMatchObject({
      dedupe: "by_document",
      mmr: { enabled: true, lambda: 0.3 },
      include_highlights: true,
      explain: true,
    });
  });

  it("renders a curl command that survives single quotes in the query", () => {
    const command = curlFor(buildQuery("kb", "it's", BASE), "https://cairn.test");
    expect(command).toContain("curl -sS -X POST 'https://cairn.test/v1/retrieval/query'");
    expect(command).toContain('Authorization: Bearer $CAIRN_API_KEY');
    expect(command).toContain("it'\\''s");
  });
});

const KB = {
  id: "kb_test",
  name: "Field notes",
  description: "",
  status: "active",
  doc_count: 1,
  chunk_count: 2,
  bytes_used: 10,
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
];

function mockApi() {
  const fetchMock = vi.fn(async (path: string, _init?: RequestInit) => {
    if (path.includes("/documents?")) return jsonResponse({ items: DOCS, next_cursor: null });
    if (path.endsWith("/documents/stats")) return jsonResponse({ counts: { indexed: 1 }, total: 1 });
    if (path.endsWith("options")) return jsonResponse({ supported_extensions: [".md"], max_upload_bytes: 1024, models: [], bindings: [], tokenizers: [] });
    if (path.endsWith("/permissions")) return jsonResponse({ workspace_permissions: [], resource_permissions: { kb_test: ["kb:manage"] } });
    if (path === "/v1/meta") return jsonResponse({ capabilities: { rerank: false } });
    if (path === "/v1/retrieval/query")
      return jsonResponse({
        request_id: "req_1",
        results: [
          {
            chunk_id: "chk_1",
            document_id: "doc_ok",
            knowledge_base_id: "kb_test",
            content: "土星 Saturn has rings.",
            score: 0.91,
            highlights: [{ start: 3, end: 9 }],
            metadata: { page: 1, lang: "en" },
            source: { title: "Saturn.md", type: "md", updated_at: "2026-09-20T00:00:00Z" },
          },
        ],
        usage: { embedding_tokens: 3, latency_ms: { total: 12 } },
        degraded: [],
        partial_failures: [],
        explain: {
          plan: { top_k: 5, targets: [{ kb: "kb_test", index_version: 2 }] },
          stages: [
            { stage: "sparse", latency_ms: 3, returned: 7, top: [{ chunk_id: "chk_1", score: 2.5, rank: 1, matched_terms: ["saturn"] }] },
            { stage: "fuse", latency_ms: 0, method: "rrf(k=60)", rank_changes: [{ chunk_id: "chk_1", dense_rank: 2, sparse_rank: 1, fused_rank: 1 }] },
          ],
        },
      });
    return jsonResponse(KB);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function mountSearch() {
  const pinia = freshPinia();
  signIn("admin", []);
  const router = makeRouter();
  await router.push("/knowledge/kb_test?tab=search");
  const wrapper = mountWith(KnowledgeDetailView, { pinia, router });
  await flushPromises();
  await settle();
  return wrapper;
}

describe("search console", () => {
  it("sends filters and ranking options and renders highlights, source and explain", async () => {
    const fetchMock = mockApi();
    const wrapper = await mountSearch();

    await wrapper.find('[data-test="toggle-advanced"]').trigger("click");
    await wrapper.find('[data-test="metadata-filter"]').setValue('{"lang": "en"}');
    await wrapper.find('[data-test="explain-toggle"] button').trigger("click");
    await wrapper.find('[data-test="retrieval-query"]').setValue("Saturn rings");
    await wrapper.find('[data-test="run-search"]').trigger("click");
    await flushPromises();
    await settle();

    const call = fetchMock.mock.calls.find(([path]) => path === "/v1/retrieval/query");
    const body = JSON.parse(String((call?.[1] as RequestInit).body));
    expect(body.filters).toEqual({ metadata: { lang: "en" } });
    expect(body.options.explain).toBe(true);
    expect(body.options.include_highlights).toBe(true);

    const marks = wrapper.findAll('[data-test="hit-content"] mark');
    expect(marks.map((mark) => mark.text())).toEqual(["Saturn"]);
    expect(wrapper.find('[data-test="search-results"]').text()).toContain("Updated");
    expect(wrapper.find('[data-test="explain-panel"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-test="explain-stage"]')).toHaveLength(2);
    expect(wrapper.find('[data-test="copy-curl"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("shows an invalid metadata filter inline without sending a request", async () => {
    const fetchMock = mockApi();
    const wrapper = await mountSearch();

    await wrapper.find('[data-test="toggle-advanced"]').trigger("click");
    await wrapper.find('[data-test="metadata-filter"]').setValue("not json");
    await wrapper.find('[data-test="retrieval-query"]').setValue("Saturn");
    await wrapper.find('[data-test="run-search"]').trigger("click");
    await flushPromises();

    expect(fetchMock.mock.calls.some(([path]) => path === "/v1/retrieval/query")).toBe(false);
    expect(wrapper.find('[data-test="filter-options"]').text()).toContain("Enter a JSON object");
    wrapper.unmount();
  });
});

describe("upload metadata", () => {
  it("attaches the metadata JSON to each uploaded file and refuses invalid JSON", async () => {
    const fetchMock = mockApi();
    fetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.endsWith("/documents/upload"))
        return jsonResponse({ filename: "a.md", status: "accepted", reason: null, document: DOCS[0], existing_document_id: null });
      if (path.includes("/documents?")) return jsonResponse({ items: DOCS, next_cursor: null });
      if (path.endsWith("/documents/stats")) return jsonResponse({ counts: { indexed: 1 }, total: 1 });
      if (path.endsWith("options")) return jsonResponse({ supported_extensions: [".md"], max_upload_bytes: 1024, models: [], bindings: [], tokenizers: [] });
      if (path.endsWith("/permissions")) return jsonResponse({ workspace_permissions: [], resource_permissions: { kb_test: ["kb:manage"] } });
      if (path === "/v1/meta") return jsonResponse({ capabilities: { rerank: false } });
      void init;
      return jsonResponse(KB);
    });
    const pinia = freshPinia();
    signIn("admin", []);
    const router = makeRouter();
    await router.push("/knowledge/kb_test");
    const wrapper = mountWith(KnowledgeDetailView, { pinia, router });
    await flushPromises();
    await settle();

    const input = wrapper.find('[data-test="upload-metadata-input"]');
    const picker = wrapper.find('[data-test="document-upload"]');
    const file = new File(["# A"], "a.md", { type: "text/markdown" });

    await input.setValue("not json");
    Object.defineProperty(picker.element, "files", { value: [file], configurable: true });
    await picker.trigger("change");
    await flushPromises();
    expect(fetchMock.mock.calls.some(([path]) => path.endsWith("/documents/upload"))).toBe(false);
    expect(wrapper.find('[data-test="upload-metadata"]').text()).toContain("Enter a JSON object");

    await input.setValue('{"lang": "en"}');
    Object.defineProperty(picker.element, "files", { value: [file], configurable: true });
    await picker.trigger("change");
    await flushPromises();
    const call = fetchMock.mock.calls.find(([path]) => path.endsWith("/documents/upload"));
    const form = (call?.[1] as RequestInit).body as FormData;
    expect(form.get("metadata")).toBe('{"lang":"en"}');
    wrapper.unmount();
  });
});
