import { ApiError } from "@/api/client";

export interface ChatEvent {
  type: "delta" | "completed" | "error";
  delta?: string;
  code?: string;
  input_tokens?: number | null;
  output_tokens?: number | null;
  model_id?: string;
}

export async function streamChat(model: string, prompt: string, receive: (event: ChatEvent) => void, signal: AbortSignal): Promise<void> {
  const csrf = document.cookie.split("; ").find((item) => item.startsWith("cairn_csrf="))?.split("=").slice(1).join("=");
  const response = await fetch(`/v1/models/${encodeURIComponent(model)}/chat`, {
    method: "POST", credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}) },
    body: JSON.stringify({ messages: [{ role: "user", content: prompt }], stream: true, max_tokens: 1024 }),
  });
  if (!response.ok) throw new ApiError(response.status, await response.json());
  if (!response.body) throw new Error("The model stream is unavailable.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "", completed = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      let boundary: number;
      while ((boundary = pending.indexOf("\n\n")) >= 0) {
        const frame = pending.slice(0, boundary); pending = pending.slice(boundary + 2);
        const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
        if (data) {
          const event = JSON.parse(data) as ChatEvent;
          receive(event);
          if (event.type === "error") throw new Error(event.code || "Model stream failed.");
          if (event.type === "completed") completed = true;
        }
      }
      if (pending.length > 2 * 1024 * 1024) throw new Error("Model event is too large.");
      if (done) break;
    }
    if (!completed) throw new Error("The model stream ended before completion.");
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
