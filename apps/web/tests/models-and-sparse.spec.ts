import { flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import CreateKnowledgeBaseSheet from "@/features/knowledge/CreateKnowledgeBaseSheet.vue";
import { autoSparseModel, parseSparseValue } from "@/features/knowledge/sparse";
import ModelsView from "@/features/models/ModelsView.vue";

import { freshPinia, jsonResponse, mountWith, settle, signIn } from "./helpers";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

type Call = { path: string; method: string; body: Record<string, unknown> | null };

function recordingFetch(routes: Record<string, unknown>) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (path: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ path, method, body: init?.body && typeof init.body === "string" ? JSON.parse(init.body) : null });
    const value = routes[`${method} ${path}`] ?? routes[path];
    return jsonResponse(value ?? []);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const MINILM = {
  id: "mdl_minilm",
  provider_id: "prov_tei",
  display_name: "MiniLM",
  model_key: "sentence-transformers/all-MiniLM-L6-v2",
  dimension: 384,
  max_input_tokens: 256,
  tokenizer_id: "minilm",
  provider_family: "tei",
  sparse: false,
};
const M3 = { ...MINILM, id: "mdl_m3", display_name: "bge-m3", provider_family: "bge_m3", sparse: true, dimension: 1024 };
const QWEN = { ...MINILM, id: "mdl_qwen", display_name: "Qwen v4", provider_family: "dashscope", sparse: true };

describe("keyword search source", () => {
  it("picks the embedding model's own sparse output, else local bge-m3 before vendors", () => {
    expect(autoSparseModel("mdl_qwen", [MINILM, M3, QWEN])?.id).toBe("mdl_qwen");
    expect(autoSparseModel("mdl_minilm", [MINILM, QWEN, M3])?.id).toBe("mdl_m3");
    expect(autoSparseModel("mdl_minilm", [MINILM])).toBeNull();
    expect(parseSparseValue("model:mdl_m3")).toEqual({ kind: "model", model_id: "mdl_m3" });
    expect(parseSparseValue("bm25")).toEqual({ kind: "bm25" });
    expect(parseSparseValue("auto")).toEqual({ kind: "auto" });
  });

  it("names the automatic choice and sends the selected source when creating", async () => {
    const pinia = freshPinia();
    signIn("admin");
    const calls = recordingFetch({
      "/v1/knowledge-base-options": {
        models: [MINILM, M3],
        bindings: [
          { id: "bind_o", kind: "object", driver: "local", name: "Files", health_state: "healthy" },
          { id: "bind_v", kind: "vector", driver: "qdrant", name: "Vectors", health_state: "healthy" },
        ],
        tokenizers: ["minilm", "bge-m3"],
        supported_extensions: [".md"],
        max_upload_bytes: 1000,
      },
      "POST /v1/knowledge-bases": { id: "kb_new", name: "Notes" },
    });
    // Options load when the sheet opens, exactly as when a user clicks "New".
    const wrapper = mountWith(CreateKnowledgeBaseSheet, { pinia, props: { open: false } });
    await wrapper.setProps({ open: true } as Record<string, unknown>);
    await flushPromises();
    await settle();

    const sheet = document.body.querySelector('[data-test="create-kb-sheet"]') as HTMLElement;
    const model = sheet.querySelector('[data-test="kb-model"]') as HTMLSelectElement;
    model.value = "mdl_minilm";
    model.dispatchEvent(new Event("change"));
    await settle();
    const sparse = sheet.querySelector('[data-test="kb-sparse"]') as HTMLSelectElement;
    expect(sparse.options[0]?.textContent).toContain("bge-m3 keyword weights");
    expect([...sparse.options].map((option) => option.value)).toEqual(["auto", "bm25", "model:mdl_m3"]);

    sparse.value = "bm25";
    sparse.dispatchEvent(new Event("change"));
    const name = sheet.querySelector('[data-test="kb-name"]') as HTMLInputElement;
    name.value = "Notes";
    name.dispatchEvent(new Event("input"));
    await settle();
    (document.body.querySelector('[data-test="kb-submit"]') as HTMLButtonElement).click();
    await flushPromises();
    await settle();

    const created = calls.find((call) => call.method === "POST" && call.path === "/v1/knowledge-bases");
    expect(created?.body?.sparse).toEqual({ kind: "bm25" });
    wrapper.unmount();
  });
});

describe("hosted providers", () => {
  it("requires an API key, never allows private networking and does not keep the key", async () => {
    const pinia = freshPinia();
    signIn("admin");
    const calls = recordingFetch({
      "/v1/knowledge-base-options": { models: [], bindings: [], tokenizers: ["bge-m3"], supported_extensions: [], max_upload_bytes: 1 },
      "POST /v1/model-providers": { id: "prov_new", name: "Aliyun", family: "dashscope", base_url: "x", is_enabled: true, model_count: 0, has_credentials: true },
    });
    const wrapper = mountWith(ModelsView, { pinia });
    await flushPromises();
    await settle();

    await wrapper.find('[data-test="add-provider"]').trigger("click");
    await settle();
    const sheet = document.body.querySelector('[data-test="provider-sheet"]') as HTMLElement;
    const family = sheet.querySelector('[data-test="provider-family"]') as HTMLSelectElement;
    family.value = "dashscope";
    family.dispatchEvent(new Event("change"));
    await settle();
    expect((sheet.querySelector('[data-test="provider-url"]') as HTMLInputElement).value).toBe(
      "https://dashscope.aliyuncs.com/api/v1",
    );
    const name = sheet.querySelector('[data-test="provider-name"]') as HTMLInputElement;
    name.value = "Aliyun";
    name.dispatchEvent(new Event("input"));
    await settle();
    const submit = document.body.querySelector('[data-test="provider-submit"]') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    const key = sheet.querySelector('[data-test="provider-key-input"]') as HTMLInputElement;
    expect(key.type).toBe("password");
    key.value = "sk-test-secret";
    key.dispatchEvent(new Event("input"));
    await settle();
    expect(submit.disabled).toBe(false);
    submit.click();
    await flushPromises();
    await settle();

    const created = calls.find((call) => call.method === "POST" && call.path === "/v1/model-providers");
    expect(created?.body).toMatchObject({
      family: "dashscope",
      api_key: "sk-test-secret",
      config: { allow_private: false, max_batch_size: 10 },
    });
    expect(document.body.innerHTML).not.toContain("sk-test-secret");
    wrapper.unmount();
  });
});
