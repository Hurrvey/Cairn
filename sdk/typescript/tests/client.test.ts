import { describe, expect, it, vi } from "vitest";
import { CairnClient, CairnError } from "../src/index.js";

const response = {
  request_id: "req_1",
  results: [],
  usage: {},
  degraded: [{ stage: "rerank", reason: "load_shed", detail: "Skipped" }],
  partial_failures: [],
  truncated_to_token_budget: false,
  explain: null,
};

describe("CairnClient", () => {
  it("sends auth and preserves typed retrieval notices", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      expect(request.url).toBe("https://example.test/v1/retrieval/query");
      expect(request.headers.get("authorization")).toBe("Bearer private-key");
      return Response.json(response);
    });
    const client = new CairnClient("https://example.test", "private-key", { fetch: fetcher });
    const result = await client.retrieve({
      targets: [{ knowledge_base_id: "kb_1" }],
      query: "test",
    });
    expect(result.degraded?.[0]?.reason).toBe("load_shed");
    expect(JSON.stringify(client)).not.toContain("private-key");
  });

  it("never follows redirects or retries mutations", async () => {
    const fetcher = vi.fn(async () => new Response(null, {
      status: 302,
      headers: { location: "https://foreign.test" },
    }));
    const client = new CairnClient("https://example.test", "key", {
      fetch: fetcher,
      maxRetries: 3,
    });
    await expect(client.deleteKnowledgeBase("kb_1")).rejects.toBeInstanceOf(CairnError);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0]?.[1]?.redirect).toBe("manual");
  });

  it("hard-cancels a stalled response body at the total deadline", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      cancel() { cancelled = true; },
    });
    const client = new CairnClient("https://example.test", "key", {
      timeoutMs: 10,
      fetch: async () => new Response(body),
    });
    await expect(client.request("GET", "/v1/meta")).rejects.toThrow(/timed out/);
    expect(cancelled).toBe(true);
  });

  it("does not await an unbounded reader cancel", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      cancel() {
        cancelled = true;
        return new Promise<void>(() => undefined);
      },
    });
    const client = new CairnClient("https://example.test", "key", {
      timeoutMs: 10,
      fetch: async () => new Response(body),
    });
    await expect(client.request("GET", "/v1/meta")).rejects.toThrow(/timed out/);
    expect(cancelled).toBe(true);
  });

  it("bounds and cancels response streams", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array([1, 2])); },
      cancel() { cancelled = true; },
    });
    const client = new CairnClient("https://example.test", "key", {
      fetch: async () => new Response(body),
    });
    await expect(client.request("GET", "/v1/meta", { responseLimit: 1 })).rejects.toThrow(
      /size limit/,
    );
    expect(cancelled).toBe(true);
  });

  it("retries query only and preserves successful batch partial failures", async () => {
    let queryCalls = 0;
    let batchCalls = 0;
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(input.toString()).pathname;
      if (path.endsWith("/query")) {
        queryCalls += 1;
        if (queryCalls === 1) return Response.json({ code: "UPSTREAM_UNAVAILABLE" }, { status: 503 });
        return Response.json(response);
      }
      batchCalls += 1;
      return Response.json({
        request_id: "req_batch",
        responses: [{ ...response, partial_failures: [{ target: "kb_1", code: "FAILED", detail: "x" }] }],
        usage: {},
      });
    });
    const client = new CairnClient("https://example.test", "key", { fetch: fetcher, maxRetries: 2 });
    await client.retrieve({ targets: [{ knowledge_base_id: "kb_1" }], query: "q" });
    const batch = await client.batch({ queries: [{ targets: [{ knowledge_base_id: "kb_1" }], query: "q" }] });
    expect(queryCalls).toBe(2);
    expect(batchCalls).toBe(1);
    expect(batch.responses[0]?.partial_failures).toHaveLength(1);
  });

  it("rejects origin bypass paths and managed header overrides", async () => {
    const client = new CairnClient("https://example.test", "key", {
      fetch: async () => Response.json({}),
    });
    await expect(client.request("GET", "https://foreign.test/path")).rejects.toBeInstanceOf(TypeError);
    await expect(client.request("GET", "//foreign.test/path")).rejects.toBeInstanceOf(TypeError);
    await expect(client.request("GET", "/v1/%2e%2e/secret")).rejects.toBeInstanceOf(TypeError);
    await expect(client.request("GET", "/v1/meta", {
      headers: { Authorization: "Bearer hostile" },
    })).rejects.toBeInstanceOf(TypeError);
  });

  it("encodes CRUD ids and emits exact multipart fields", async () => {
    const requests: Request[] = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      requests.push(request);
      if (request.url.endsWith("/documents/upload")) {
        const form = await request.formData();
        expect(form.get("metadata")).toBe('{"lang":"en"}');
        expect((form.get("file") as File).name).toBe("guide.md");
        return Response.json({ filename: "guide.md", status: "accepted", document: null }, { status: 202 });
      }
      return Response.json({}, { status: 202 });
    });
    const client = new CairnClient("https://example.test", "key", { fetch: fetcher });
    await client.uploadDocument("kb/1", new Blob(["# Guide"]), "guide.md", { lang: "en" });
    await client.deleteDocument("kb/1", "doc?secret=1");
    expect(new URL(requests[0]!.url).pathname).toBe("/v1/knowledge-bases/kb%2F1/documents/upload");
    expect(new URL(requests[1]!.url).pathname).toBe(
      "/v1/knowledge-bases/kb%2F1/documents/doc%3Fsecret%3D1",
    );
  });

  it("preserves a base path, omits browser credentials, and handles 204", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => new Response(null, { status: 204 }));
    const client = new CairnClient("https://example.test/cairn", "key", { fetch: fetcher });
    await expect(client.deleteKnowledgeBase("kb_1")).resolves.toEqual({});
    expect(fetcher.mock.calls[0]?.[0].toString()).toBe(
      "https://example.test/cairn/v1/knowledge-bases/kb_1",
    );
    expect(fetcher.mock.calls[0]?.[1]?.credentials).toBe("omit");
    expect(fetcher.mock.calls[0]?.[1]?.referrerPolicy).toBe("no-referrer");
  });

  it("validates response limits and redacts document metadata", async () => {
    const client = new CairnClient("https://example.test", "private-key", {
      fetch: async () => new Response("# Guide", {
        headers: {
          "Content-Type": "text/markdown",
          "X-Request-Id": "echo-private-key",
          "X-Document-Revision": "2",
        },
      }),
    });
    await expect(client.request("GET", "/v1/meta", { responseLimit: Number.NaN }))
      .rejects.toBeInstanceOf(TypeError);
    await expect(client.documentContent("doc_1")).resolves.toEqual({
      text: "# Guide",
      requestId: undefined,
      revision: 2,
    });
  });

  it("passes caller abort signals through typed document operations", async () => {
    const controller = new AbortController();
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      controller.abort();
      await new Promise((resolve) => setTimeout(resolve, 20));
      return Response.json({});
    });
    const client = new CairnClient("https://example.test", "key", { fetch: fetcher });
    await expect(client.getDocument("kb_1", "doc_1", controller.signal)).rejects.toThrow(
      /timed out or was cancelled/,
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("stops paginating when the caller aborts", async () => {
    const controller = new AbortController();
    const fetcher = vi.fn(async () => Response.json({
      items: [{ id: "kb_1" }],
      has_more: true,
      next_cursor: "next",
    }));
    const client = new CairnClient("https://example.test", "key", { fetch: fetcher });
    const iterator = client.iterateKnowledgeBases(1, controller.signal);
    await expect(iterator.next()).resolves.toMatchObject({ value: { id: "kb_1" } });
    controller.abort();
    await expect(iterator.next()).rejects.toThrow(/cancelled/);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
