import type { components } from "./schema.js";

type GeneratedRetrievalRequest = components["schemas"]["RetrievalRequest"];
type GeneratedRetrievalTarget = components["schemas"]["RetrievalTarget"];
export type RetrievalRequest = Omit<GeneratedRetrievalRequest, "targets" | "strict"> & {
  targets: Array<Omit<GeneratedRetrievalTarget, "weight"> & { weight?: number }>;
  strict?: boolean;
};
export type RetrievalResponse = components["schemas"]["RetrievalResponse"];
export type BatchRequest = Omit<components["schemas"]["BatchRequest"], "queries"> & {
  queries: RetrievalRequest[];
};
export type BatchResponse = components["schemas"]["BatchResponse"];
export type KnowledgeBase = components["schemas"]["KnowledgeBaseResponse"];
export type KnowledgeBasePage = components["schemas"]["CursorPage_KnowledgeBaseResponse_"];
export type CreateKnowledgeBaseRequest = components["schemas"]["CreateKbRequest"];
export type UpdateKnowledgeBaseRequest = components["schemas"]["UpdateKbRequest"];
export type Document = components["schemas"]["DocumentResponse"];
export type DocumentPage = components["schemas"]["CursorPage_DocumentResponse_"];
export type DocumentRegistration = components["schemas"]["DocumentRegistrationResponse"];
export type Chunk = components["schemas"]["ChunkResponse"];
export type CitedChunk = components["schemas"]["CitedChunkResponse"];
export type EditChunkRequest = components["schemas"]["EditChunkRequest"];

const JSON_LIMIT = 8 * 1024 * 1024;
const CONTENT_LIMIT = 20 * 1024 * 1024;
const UPLOAD_LIMIT = 50 * 1024 * 1024;
const ERROR_LIMIT = 64 * 1024;
const RETRYABLE_STATUS = new Set([429, 502, 503, 504]);
const RETRYABLE_POST_PATHS = new Set(["/v1/retrieval/query", "/v1/retrieval/batch"]);

export class CairnError extends Error {
  readonly statusCode: number | undefined;
  readonly code: string | undefined;
  readonly requestId: string | undefined;
  readonly retryAfter: number | undefined;

  constructor(message: string, options: {
    statusCode?: number | undefined;
    code?: string | undefined;
    requestId?: string | undefined;
    retryAfter?: number | undefined;
  } = {}) {
    super(message);
    this.name = "CairnError";
    this.statusCode = options.statusCode;
    this.code = options.code;
    this.requestId = options.requestId;
    this.retryAfter = options.retryAfter;
  }

  toJSON(): Record<string, unknown> {
    return {
      name: this.name,
      message: this.message,
      statusCode: this.statusCode,
      code: this.code,
      requestId: this.requestId,
      retryAfter: this.retryAfter,
    };
  }
}

export interface CairnClientOptions {
  fetch?: typeof fetch;
  timeoutMs?: number;
  maxRetries?: number;
}

export interface RequestOptions {
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  headers?: HeadersInit;
  signal?: AbortSignal | undefined;
  responseLimit?: number;
  retry?: boolean;
}

export interface DocumentContent {
  text: string;
  requestId?: string | undefined;
  revision?: number | undefined;
}

export class CairnClient {
  #baseUrl: URL;
  #apiKey: string;
  #fetch: typeof fetch;
  #timeoutMs: number;
  #maxRetries: number;

  constructor(baseUrl: string, apiKey: string, options: CairnClientOptions = {}) {
    this.#baseUrl = validateBaseUrl(baseUrl);
    if (!apiKey.trim()) throw new TypeError("apiKey must not be empty");
    const timeoutMs = options.timeoutMs ?? 30_000;
    const maxRetries = options.maxRetries ?? 0;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new TypeError("timeoutMs must be finite and positive");
    }
    if (!Number.isInteger(maxRetries) || maxRetries < 0) {
      throw new TypeError("maxRetries must be a non-negative integer");
    }
    this.#apiKey = apiKey;
    this.#fetch = options.fetch ?? globalThis.fetch;
    this.#timeoutMs = timeoutMs;
    this.#maxRetries = maxRetries;
  }

  toJSON(): Record<string, unknown> {
    return { baseUrl: this.#baseUrl.href.replace(/\/$/, "") };
  }

  async request(method: string, path: string, options: RequestOptions = {}): Promise<Response> {
    const normalizedMethod = method.toUpperCase();
    validatePath(path);
    const responseLimit = options.responseLimit ?? JSON_LIMIT;
    if (!Number.isSafeInteger(responseLimit) || responseLimit <= 0) {
      throw new TypeError("responseLimit must be a positive safe integer");
    }
    const url = new URL(`${this.#baseUrl.pathname.replace(/\/$/, "")}${path}`, this.#baseUrl.origin);
    for (const [key, value] of Object.entries(options.query ?? {})) {
      if (value !== null && value !== undefined) url.searchParams.set(key, String(value));
    }
    const supplied = new Headers(options.headers);
    if (supplied.has("authorization") || supplied.has("host")) {
      throw new TypeError("Authorization and Host headers are managed by CairnClient");
    }
    const declaredRetryable = normalizedMethod === "GET" || normalizedMethod === "HEAD"
      || (normalizedMethod === "POST" && RETRYABLE_POST_PATHS.has(path));
    const attempts = declaredRetryable && options.retry !== false ? this.#maxRetries + 1 : 1;
    const deadline = performance.now() + this.#timeoutMs;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new CairnError("Cairn request exceeded its total deadline.");
      const { signal, cleanup } = mergedSignal(options.signal, remaining);
      let response: Response;
      try {
        const headers = new Headers(supplied);
        headers.set("Authorization", `Bearer ${this.#apiKey}`);
        headers.set("Accept", headers.get("Accept") ?? "application/json");
        if (options.body !== undefined && !(options.body instanceof FormData)) {
          headers.set("Content-Type", "application/json");
        }
        const init: RequestInit = {
          method: normalizedMethod,
          headers,
          redirect: "manual",
          signal,
          credentials: "omit",
          referrerPolicy: "no-referrer",
        };
        if (options.body !== undefined) {
          init.body = options.body instanceof FormData ? options.body : JSON.stringify(options.body);
        }
        response = await this.#fetch(url, init);
        if (response.type === "opaqueredirect") {
          throw new CairnError("Cairn redirects are disabled.");
        }
        response = await boundedResponse(
          response,
          response.ok ? responseLimit : Math.min(responseLimit, ERROR_LIMIT),
          signal,
        );
      } catch (error) {
        cleanup();
        if (signal.aborted) throw new CairnError("Cairn request timed out or was cancelled.");
        if (error instanceof CairnError) throw error;
        if (attempt + 1 >= attempts) throw new CairnError("Cairn transport failed.");
        continue;
      }
      cleanup();
      if (response.status >= 300 && response.status < 400) {
        throw new CairnError("Cairn redirects are disabled.", {
          statusCode: response.status,
          requestId: safeRequestId(response, this.#apiKey),
        });
      }
      if (response.ok) return response;
      const failure = await responseError(response, this.#apiKey);
      if (attempt + 1 >= attempts || !RETRYABLE_STATUS.has(response.status)) throw failure;
      const delay = failure.retryAfter ?? 0;
      if (delay * 1000 >= deadline - performance.now()) throw failure;
      if (delay > 0) await abortableDelay(delay * 1000, options.signal);
    }
    throw new Error("unreachable");
  }

  async retrieve(request: RetrievalRequest, signal?: AbortSignal): Promise<RetrievalResponse> {
    return this.#json("POST", "/v1/retrieval/query", { body: request, signal });
  }

  async batch(request: BatchRequest, signal?: AbortSignal): Promise<BatchResponse> {
    return this.#json("POST", "/v1/retrieval/batch", { body: request, signal });
  }

  async listKnowledgeBases(limit = 50, cursor?: string, signal?: AbortSignal): Promise<KnowledgeBasePage> {
    return this.#json("GET", "/v1/knowledge-bases", { query: { limit, cursor }, signal });
  }

  async *iterateKnowledgeBases(pageSize = 50, signal?: AbortSignal): AsyncGenerator<KnowledgeBase> {
    let cursor: string | undefined;
    const seen = new Set<string>();
    while (true) {
      if (signal?.aborted) throw new CairnError("Cairn request was cancelled.");
      const page = await this.listKnowledgeBases(pageSize, cursor, signal);
      yield* page.items;
      if (!page.has_more || page.next_cursor == null) return;
      if (seen.has(page.next_cursor)) throw new CairnError("Cairn returned a repeated cursor.");
      seen.add(page.next_cursor);
      cursor = page.next_cursor;
    }
  }

  getKnowledgeBase(id: string, signal?: AbortSignal): Promise<KnowledgeBase> {
    return this.#json("GET", `/v1/knowledge-bases/${segment(id)}`, { signal });
  }

  createKnowledgeBase(request: CreateKnowledgeBaseRequest, signal?: AbortSignal): Promise<KnowledgeBase> {
    return this.#json("POST", "/v1/knowledge-bases", { body: request, signal });
  }

  updateKnowledgeBase(
    id: string,
    request: UpdateKnowledgeBaseRequest,
    signal?: AbortSignal,
  ): Promise<KnowledgeBase> {
    return this.#json("PATCH", `/v1/knowledge-bases/${segment(id)}`, { body: request, signal });
  }

  deleteKnowledgeBase(id: string, signal?: AbortSignal): Promise<unknown> {
    return this.#json("DELETE", `/v1/knowledge-bases/${segment(id)}`, { signal });
  }

  listDocuments(
    kbId: string,
    limit = 50,
    cursor?: string,
    signal?: AbortSignal,
  ): Promise<DocumentPage> {
    return this.#json("GET", `/v1/knowledge-bases/${segment(kbId)}/documents`, {
      query: { limit, cursor },
      signal,
    });
  }

  getDocument(kbId: string, documentId: string, signal?: AbortSignal): Promise<Document> {
    return this.#json(
      "GET",
      `/v1/knowledge-bases/${segment(kbId)}/documents/${segment(documentId)}`,
      { signal },
    );
  }

  uploadDocument(
    kbId: string,
    file: Blob,
    filename: string,
    metadata?: unknown,
    signal?: AbortSignal,
  ): Promise<DocumentRegistration> {
    if (file.size > UPLOAD_LIMIT) {
      return Promise.reject(new CairnError("Document upload exceeds the upload size limit."));
    }
    const form = new FormData();
    form.set("file", file, filename);
    if (metadata !== undefined) form.set("metadata", JSON.stringify(metadata));
    return this.#json("POST", `/v1/knowledge-bases/${segment(kbId)}/documents/upload`, {
      body: form,
      signal,
    });
  }

  retryDocument(kbId: string, documentId: string, signal?: AbortSignal): Promise<unknown> {
    return this.#json(
      "POST",
      `/v1/knowledge-bases/${segment(kbId)}/documents/${segment(documentId)}/retry`,
      { signal },
    );
  }

  deleteDocument(kbId: string, documentId: string, signal?: AbortSignal): Promise<unknown> {
    return this.#json(
      "DELETE",
      `/v1/knowledge-bases/${segment(kbId)}/documents/${segment(documentId)}`,
      { signal },
    );
  }

  listChunks(kbId: string, documentId: string, options: {
    indexVersion?: number;
    limit?: number;
    afterOrdinal?: number;
    signal?: AbortSignal;
  } = {}): Promise<Chunk[]> {
    return this.#json("GET", `/v1/knowledge-bases/${segment(kbId)}/documents/${segment(documentId)}/chunks`, {
      query: {
        index_version: options.indexVersion,
        limit: options.limit ?? 50,
        after_ordinal: options.afterOrdinal,
      },
      signal: options.signal,
    });
  }

  editChunk(
    kbId: string,
    chunkId: string,
    request: EditChunkRequest,
    signal?: AbortSignal,
  ): Promise<Chunk> {
    return this.#json("PATCH", `/v1/knowledge-bases/${segment(kbId)}/chunks/${segment(chunkId)}`, {
      body: request,
      signal,
    });
  }

  getChunk(chunkId: string, signal?: AbortSignal): Promise<CitedChunk> {
    return this.#json("GET", `/v1/chunks/${segment(chunkId)}`, { signal });
  }

  async documentContent(documentId: string, signal?: AbortSignal): Promise<DocumentContent> {
    const response = await this.request("GET", `/v1/documents/${segment(documentId)}/content`, {
      headers: { Accept: "text/markdown, text/plain" },
      responseLimit: CONTENT_LIMIT,
      signal,
    });
    const rawRevision = response.headers.get("x-document-revision");
    const revision = rawRevision == null ? undefined : Number(rawRevision);
    return {
      text: await response.text(),
      requestId: safeRequestId(response, this.#apiKey),
      revision: Number.isInteger(revision) ? revision : undefined,
    };
  }

  async #json<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const response = await this.request(method, path, options);
    if (response.status === 204 || response.headers.get("content-length") === "0") return {} as T;
    try {
      return await response.json() as T;
    } catch {
      throw new CairnError("Cairn returned an invalid JSON response.");
    }
  }
}

function validateBaseUrl(value: string): URL {
  const url = new URL(value);
  if (!new Set(["http:", "https:"]).has(url.protocol) || url.username || url.password
      || url.search || url.hash) {
    throw new TypeError("baseUrl must be http(s) without credentials, query, or fragment");
  }
  url.pathname = url.pathname.replace(/\/$/, "") + "/";
  return url;
}

function validatePath(path: string): void {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("?") || path.includes("#")
      || path.includes("\\")) throw new TypeError("request path is unsafe");
  for (const raw of path.slice(1).split("/")) {
    const decoded = decodeURIComponent(raw);
    if (!decoded || decoded === "." || decoded === "..") throw new TypeError("request path is unsafe");
  }
}

function segment(value: string): string {
  if (!value) throw new TypeError("path segment must not be empty");
  return encodeURIComponent(value);
}

function mergedSignal(userSignal: AbortSignal | undefined, timeoutMs: number): {
  signal: AbortSignal;
  cleanup: () => void;
} {
  const controller = new AbortController();
  const abort = (): void => controller.abort(userSignal?.reason);
  if (userSignal?.aborted) abort();
  else userSignal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(() => controller.abort(new DOMException("Timeout", "TimeoutError")), timeoutMs);
  return {
    signal: controller.signal,
    cleanup: () => {
      clearTimeout(timer);
      userSignal?.removeEventListener("abort", abort);
    },
  };
}

async function boundedResponse(response: Response, limit: number, signal: AbortSignal): Promise<Response> {
  const reader = response.body?.getReader();
  if (!reader) return copiedResponse(response, null);
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      if (signal.aborted) throw signal.reason;
      const { done, value } = await readWithSignal(reader, signal);
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new CairnError("Cairn response exceeded the size limit.");
      chunks.push(value);
    }
  } finally {
    void reader.cancel().catch(() => undefined);
  }
  return copiedResponse(response, new Blob(chunks));
}

function copiedResponse(response: Response, body: BodyInit | null): Response {
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

async function readWithSignal(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  signal: AbortSignal,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  if (signal.aborted) throw signal.reason;
  let abort: (() => void) | undefined;
  const aborted = new Promise<never>((_, reject) => {
    abort = () => reject(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
  });
  try {
    return await Promise.race([reader.read(), aborted]);
  } finally {
    if (abort) signal.removeEventListener("abort", abort);
  }
}

async function responseError(response: Response, apiKey: string): Promise<CairnError> {
  let code: string | undefined;
  let bodyRequestId: string | undefined;
  if ((response.headers.get("content-type") ?? "").includes("json")) {
    try {
      const value = await response.json() as Record<string, unknown>;
      if (typeof value.code === "string" && value.code.length <= 128 && !value.code.includes(apiKey)) {
        code = value.code;
      }
      if (typeof value.request_id === "string" && value.request_id.length <= 256
          && !value.request_id.includes(apiKey)) {
        bodyRequestId = value.request_id;
      }
    } catch { /* bounded response was malformed */ }
  }
  return new CairnError(`Cairn request failed with HTTP ${response.status}.`, {
    statusCode: response.status,
    code,
    requestId: safeRequestId(response, apiKey) ?? bodyRequestId,
    retryAfter: retryAfter(response.headers.get("retry-after")),
  });
}

function safeRequestId(response: Response, apiKey: string): string | undefined {
  const value = response.headers.get("x-request-id");
  return value && value.length <= 256 && !value.includes(apiKey) ? value : undefined;
}

function retryAfter(value: string | null): number | undefined {
  if (value == null) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds);
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, (date - Date.now()) / 1000) : undefined;
}

async function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) throw new CairnError("Cairn request was cancelled.");
  await new Promise<void>((resolve, reject) => {
    const finish = (): void => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    const abort = (): void => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(new CairnError("Cairn request was cancelled."));
    };
    signal?.addEventListener("abort", abort, { once: true });
  });
}
