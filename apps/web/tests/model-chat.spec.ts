import { describe, expect, it, vi, afterEach } from "vitest";
import { streamChat } from "@/api/model-chat";

afterEach(() => vi.unstubAllGlobals());

describe("model streaming", () => {
  it("reads split SSE events and returns completion usage", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream({ start(controller) {
      controller.enqueue(encoder.encode('event: delta\ndata: {"type":"delta","delta":"hel'));
      controller.enqueue(encoder.encode('lo"}\n\nevent: completed\ndata: {"type":"completed","input_tokens":3,"output_tokens":1}\n\n'));
      controller.close();
    } });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status: 200 })));
    const events: unknown[] = [];
    await streamChat("mdl_test", "Hi", (event) => events.push(event), new AbortController().signal);
    expect(events).toEqual([{ type: "delta", delta: "hello" }, { type: "completed", input_tokens: 3, output_tokens: 1 }]);
  });
});
