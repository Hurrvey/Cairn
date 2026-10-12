import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "@/api/client";

afterEach(() => vi.unstubAllGlobals());

describe("document uploads", () => {
  it("sends multipart bytes without JSON encoding or a fabricated boundary", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        new Response('{"status":"accepted"}', { status: 200 }),
      );
    vi.stubGlobal("fetch", fetcher);
    const body = new FormData();
    body.append(
      "file",
      new File(["# Document"], "document.md", { type: "text/markdown" }),
    );
    await request("/v1/knowledge-bases/kb_test/documents/upload", {
      method: "POST",
      body,
    });
    expect(fetcher.mock.calls[0]?.[1].body).toBe(body);
    expect(fetcher.mock.calls[0]?.[1].headers["Content-Type"]).toBeUndefined();
  });
});
