/**
 * Provider families and model presets for the models page.
 *
 * Mirrors the server's family rules (cairn.core.provider_runtime): hosted
 * vendors require an API key and never use private networking; only bge-m3 and
 * DashScope return learned sparse vectors. The server enforces all of this;
 * the page only chooses sensible defaults.
 */

export type Family = "tei" | "infinity" | "bge_m3" | "dashscope" | "volcengine" | "openai_compatible";

export interface FamilySpec {
  baseUrl: string;
  batch: number;
  /** "required" for hosted vendors, "optional" where a key is possible, "hidden" otherwise. */
  key: "required" | "optional" | "hidden";
  /** Runs inside the deployment's network, so private addresses are allowed. */
  local: boolean;
  sparse: boolean;
  sendsDimension: boolean;
}

export const FAMILIES: Record<Family, FamilySpec> = {
  tei: { baseUrl: "http://embedding:80", batch: 16, key: "hidden", local: true, sparse: false, sendsDimension: false },
  bge_m3: { baseUrl: "http://bge-m3:8000", batch: 16, key: "optional", local: true, sparse: true, sendsDimension: false },
  dashscope: {
    baseUrl: "https://dashscope.aliyuncs.com/api/v1",
    batch: 10,
    key: "required",
    local: false,
    sparse: true,
    sendsDimension: true,
  },
  volcengine: {
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    batch: 16,
    key: "required",
    local: false,
    sparse: false,
    sendsDimension: true,
  },
  openai_compatible: {
    baseUrl: "https://api.openai.com/v1",
    batch: 16,
    key: "optional",
    local: true,
    sparse: false,
    sendsDimension: true,
  },
  infinity: { baseUrl: "", batch: 16, key: "hidden", local: true, sparse: false, sendsDimension: false },
};

export interface ModelPreset {
  id: string;
  label: string;
  model_key: string;
  display_name: string;
  /** null: detect from the provider on registration. */
  dimension: number | null;
  max_input_tokens: number;
  tokenizer_id: string;
  batch: number;
  sparse?: boolean;
  send_dimension?: boolean;
  query_prefix?: string;
}

export const MODEL_PRESETS: Partial<Record<Family, ModelPreset[]>> = {
  tei: [
    {
      id: "minilm",
      label: "all-MiniLM-L6-v2 · 384d",
      model_key: "sentence-transformers/all-MiniLM-L6-v2",
      display_name: "MiniLM",
      dimension: 384,
      max_input_tokens: 256,
      tokenizer_id: "minilm",
      batch: 16,
    },
  ],
  bge_m3: [
    {
      id: "bge-m3",
      label: "BAAI/bge-m3 · 1024d · sparse",
      model_key: "BAAI/bge-m3",
      display_name: "bge-m3",
      dimension: 1024,
      max_input_tokens: 8192,
      tokenizer_id: "bge-m3",
      batch: 16,
      sparse: true,
    },
  ],
  dashscope: [
    {
      id: "text-embedding-v4",
      label: "text-embedding-v4 · 1024d · sparse",
      model_key: "text-embedding-v4",
      display_name: "Qwen text-embedding-v4",
      dimension: 1024,
      max_input_tokens: 8192,
      tokenizer_id: "bge-m3",
      batch: 10,
      sparse: true,
      send_dimension: true,
    },
    {
      id: "text-embedding-v3",
      label: "text-embedding-v3 · 1024d · sparse",
      model_key: "text-embedding-v3",
      display_name: "Qwen text-embedding-v3",
      dimension: 1024,
      max_input_tokens: 8192,
      tokenizer_id: "bge-m3",
      batch: 10,
      sparse: true,
      send_dimension: true,
    },
  ],
  volcengine: [
    {
      id: "doubao",
      label: "Doubao embedding",
      model_key: "",
      display_name: "Doubao embedding",
      dimension: null,
      max_input_tokens: 4096,
      tokenizer_id: "bge-m3",
      batch: 16,
    },
  ],
  openai_compatible: [
    {
      id: "text-embedding-3-small",
      label: "text-embedding-3-small · 1536d",
      model_key: "text-embedding-3-small",
      display_name: "OpenAI text-embedding-3-small",
      dimension: 1536,
      max_input_tokens: 8191,
      tokenizer_id: "bge-m3",
      batch: 16,
    },
    {
      id: "text-embedding-3-large",
      label: "text-embedding-3-large · 3072d",
      model_key: "text-embedding-3-large",
      display_name: "OpenAI text-embedding-3-large",
      dimension: 3072,
      max_input_tokens: 8191,
      tokenizer_id: "bge-m3",
      batch: 16,
    },
  ],
};
