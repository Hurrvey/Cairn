import { api, request } from "./client";
import type { components } from "./schema";

export type KnowledgeBase = components["schemas"]["KnowledgeBaseResponse"];
export type DocumentRecord = components["schemas"]["DocumentResponse"];
export type ChunkRecord = components["schemas"]["ChunkResponse"];
export type ModelChoice = components["schemas"]["ModelChoiceResponse"];
export type BindingChoice = components["schemas"]["SafeBindingResponse"] & {
  health_state?: string;
};
export type SetupOptions = components["schemas"]["KnowledgeBaseOptionsResponse"];
export type RetrievalConfig = components["schemas"]["RetrievalConfig"];
export type ChunkConfig = components["schemas"]["ChunkConfig"];
export type CreateKbRequest = components["schemas"]["CreateKbRequest"];
export type SparseChoice = components["schemas"]["SparseChoiceRequest"];
export type UpdateKbRequest = components["schemas"]["UpdateKbRequest"];
export type IndexProgress = components["schemas"]["IndexProgressResponse"];
export type IndexVersion = components["schemas"]["IndexVersionResponse"];
export type DocumentStateCounts = components["schemas"]["DocumentStateCounts"];
export type ReindexEstimate = components["schemas"]["ReindexEstimateResponse"];
export type RetrievalRequest = components["schemas"]["RetrievalRequest"];
export type RetrievalResult = components["schemas"]["RetrievalResponse"];
export type RetrievalHit = components["schemas"]["RetrievalHit"];
export type DocumentRegistration = components["schemas"]["DocumentRegistrationResponse"];

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

/** What the search console lets a person vary per query. */
export interface QueryOptions {
  mode: "hybrid" | "vector" | "fulltext";
  topK: number;
  fusion: "rrf" | "weighted";
  dense: number;
  sparse: number;
  expandParent: boolean;
  rerank: boolean;
  rerankModel: string;
  rerankTopN: number;
}

const base = (id: string) => `/v1/knowledge-bases/${encodeURIComponent(id)}`;
const doc = (id: string, documentId: string) =>
  `${base(id)}/documents/${encodeURIComponent(documentId)}`;

export function buildQuery(id: string, query: string, options: QueryOptions): RetrievalRequest {
  // The generated request type spells out every server default, so the
  // body below states them explicitly rather than relying on omission.
  const body: RetrievalRequest = {
    targets: [{ knowledge_base_id: id, weight: 1 }],
    query,
    search_mode: options.mode,
    top_k: options.topK,
    options: {
      expand_parent: options.expandParent,
      include_metadata: true,
      include_highlights: false,
      explain: false,
    },
    rerank: { enabled: false, top_n: options.rerankTopN, timeout_s: 1.5 },
    strict: false,
  };
  if (options.mode === "hybrid") {
    body.fusion = { method: options.fusion, k: 60 };
    if (options.fusion === "weighted") body.weights = { dense: options.dense, sparse: options.sparse };
  }
  if (options.rerank && options.rerankModel.trim()) {
    body.rerank = { enabled: true, model_id: options.rerankModel.trim(), top_n: options.rerankTopN, timeout_s: 1.5 };
  }
  return body;
}

export const knowledge = {
  list: (cursor?: string, signal?: AbortSignal) =>
    api.get<Page<KnowledgeBase>>(
      `/v1/knowledge-bases?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  options: (signal?: AbortSignal) => api.get<SetupOptions>("/v1/knowledge-base-options", { signal }),
  create: (body: CreateKbRequest) => api.post<KnowledgeBase>("/v1/knowledge-bases", body),
  get: (id: string, signal?: AbortSignal) => api.get<KnowledgeBase>(base(id), { signal }),
  update: (id: string, body: UpdateKbRequest) => api.patch<KnowledgeBase>(base(id), body),
  remove: (id: string) => api.delete<void>(base(id)),
  stats: (id: string, signal?: AbortSignal) =>
    api.get<DocumentStateCounts>(`${base(id)}/documents/stats`, { signal }),
  indexProgress: (id: string, signal?: AbortSignal) =>
    api.get<IndexProgress>(`${base(id)}/index-progress`, { signal }),
  indexVersions: (id: string, signal?: AbortSignal) =>
    api.get<IndexVersion[]>(`${base(id)}/index-versions`, { signal }),
  documents: (id: string, signal?: AbortSignal, cursor?: string) =>
    api.get<Page<DocumentRecord>>(
      `${base(id)}/documents?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  document: (id: string, documentId: string, signal?: AbortSignal) =>
    api.get<DocumentRecord>(doc(id, documentId), { signal }),
  upload: (id: string, file: File, signal?: AbortSignal) => {
    const body = new FormData();
    body.append("file", file);
    return request<DocumentRegistration>(`${base(id)}/documents/upload`, { method: "POST", body, signal });
  },
  retry: (id: string, documentId: string) => api.post<void>(`${doc(id, documentId)}/retry`),
  deleteDocument: (id: string, documentId: string) => api.delete<void>(doc(id, documentId)),
  download: (id: string, documentId: string) => `${doc(id, documentId)}/download`,
  chunks: (id: string, documentId: string, after?: number, signal?: AbortSignal) =>
    api.get<ChunkRecord[]>(
      `${doc(id, documentId)}/chunks?limit=100${after === undefined ? "" : `&after_ordinal=${after}`}`,
      { signal },
    ),
  editChunk: (id: string, chunk: string, content: string) =>
    api.patch<ChunkRecord>(`${base(id)}/chunks/${encodeURIComponent(chunk)}`, { content }),
  reindexEstimate: (id: string, sparse?: SparseChoice) =>
    api.post<ReindexEstimate>(`${base(id)}/reindex`, { confirm: false, ...(sparse ? { sparse } : {}) }),
  reindex: (id: string, sparse?: SparseChoice) =>
    api.post<Record<string, unknown>>(`${base(id)}/reindex`, { confirm: true, ...(sparse ? { sparse } : {}) }),
  breakGlass: (id: string, reason: string, ttlMinutes?: number | null) =>
    api.post<unknown>(`${base(id)}/break-glass`, { reason, ttl_minutes: ttlMinutes ?? null }),
  query: (id: string, query: string, options: QueryOptions, signal?: AbortSignal) =>
    api.post<RetrievalResult>("/v1/retrieval/query", buildQuery(id, query, options), { signal }),
};
