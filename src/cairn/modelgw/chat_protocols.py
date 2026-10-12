"""Native provider wire conversion, confined to text-only capabilities."""

from typing import Any
from urllib.parse import quote

from cairn.modelgw.invocation import ChatRequest

CHAT_FAMILIES = frozenset(
    {
        "openai",
        "openai_compatible",
        "deepseek",
        "vllm",
        "dashscope",
        "anthropic",
        "google",
        "ollama",
    }
)
RERANK_FAMILIES = frozenset({"tei", "infinity"})


def chat_body(
    family: str, model: str, request: ChatRequest, *, stream: bool
) -> tuple[str, dict[str, Any]]:
    messages = [message.model_dump() for message in request.messages]
    if family == "anthropic":
        return "/messages", {
            "model": model,
            "messages": [m for m in messages if m["role"] != "system"],
            "system": "\n\n".join(m["content"] for m in messages if m["role"] == "system"),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": stream,
        }
    if family == "google":
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "model" if m["role"] == "assistant" else "user",
                    "parts": [{"text": m["content"]}],
                }
                for m in messages
                if m["role"] != "system"
            ],
            "generationConfig": {
                "maxOutputTokens": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        system = [m["content"] for m in messages if m["role"] == "system"]
        if system:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"/models/{quote(model, safe='')}:{action}", body
    if family == "ollama":
        return "/api/chat", {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "stream": stream,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    return "/chat/completions", body


def tokens(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def chat_event(
    family: str,
    body: dict[str, Any],
    *,
    stream: bool,
) -> tuple[str, str | None, int | None, int | None, bool]:
    if not isinstance(body, dict):
        raise ValueError("provider response must be an object")
    try:
        return _chat_event(family, body, stream=stream)
    except (AttributeError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("invalid provider message structure") from exc


def _chat_event(
    family: str, body: dict[str, Any], *, stream: bool
) -> tuple[str, str | None, int | None, int | None, bool]:
    """Text delta, finish reason, input/output tokens, terminal marker."""
    if "error" in body or body.get("type") == "error":
        raise ValueError("provider error event")
    text, reason, inp, out, done = "", None, None, None, False
    if family == "anthropic":
        kind = body.get("type")
        usage = body.get("usage") or body.get("message", {}).get("usage", {})
        inp, out = tokens(usage.get("input_tokens")), tokens(usage.get("output_tokens"))
        if not stream:
            if (
                not isinstance(body.get("content"), list)
                or not body["content"]
                or any(item.get("type") != "text" for item in body["content"])
            ):
                raise ValueError("unsupported or missing text content")
            text = "".join(item["text"] for item in body["content"] if item.get("type") == "text")
            reason, done = body.get("stop_reason"), True
        elif kind == "content_block_delta":
            delta = body["delta"]
            if delta.get("type") != "text_delta":
                raise ValueError("non-text message delta")
            text = delta["text"]
        elif kind == "content_block_start" and body["content_block"].get("type") != "text":
            raise ValueError("unsupported message block")
        elif kind == "message_delta":
            reason = body["delta"].get("stop_reason")
        elif kind == "message_stop":
            done = True
    elif family == "google":
        candidates = body.get("candidates", [])
        if not candidates and not stream:
            raise ValueError("missing generation candidates")
        if candidates:
            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts", [])
            if any(
                set(part) - {"text", "thought", "thoughtSignature"} or part.get("thought") is True
                for part in parts
            ):
                raise ValueError("non-text generation")
            text = "".join(part.get("text", "") for part in parts)
            reason = candidate.get("finishReason")
        usage = body.get("usageMetadata", {})
        inp, out = tokens(usage.get("promptTokenCount")), tokens(usage.get("candidatesTokenCount"))
        done = bool(reason) or not stream
    elif family == "ollama":
        if not isinstance(body.get("message"), dict):
            raise ValueError("missing message")
        text = body.get("message", {}).get("content", "")
        if body.get("message", {}).get("tool_calls"):
            raise ValueError("non-text generation")
        done, reason = body.get("done") is True, body.get("done_reason")
        inp, out = tokens(body.get("prompt_eval_count")), tokens(body.get("eval_count"))
    else:
        choices = body.get("choices", [])
        if not choices and not stream:
            raise ValueError("missing completion choices")
        if choices:
            choice = choices[0]
            message = choice.get("delta" if stream else "message", {})
            if (
                message.get("tool_calls")
                or message.get("function_call")
                or "audio" in message
                or (not stream and not isinstance(message.get("content"), str))
            ):
                raise ValueError("non-text generation")
            text = message.get("content") or ""
            reason = choice.get("finish_reason")
        usage = body.get("usage") or {}
        inp, out = tokens(usage.get("prompt_tokens")), tokens(usage.get("completion_tokens"))
        done = bool(reason) or not stream
    if not isinstance(text, str) or (reason is not None and not isinstance(reason, str)):
        raise ValueError("invalid text output")
    return text, reason, inp, out, done
