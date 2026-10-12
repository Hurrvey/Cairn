> September 29: MCP now includes directory, filtered search, ordered document passages and resources.
> See [current tools and access requirements](09-s3-and-external-clients.md#mcp-directory-and-indexed-passages).
> The search-only limitations below describe the original increment.

# Generic Cairn MCP quickstart

Date: 2026-09-17. Read-only search MCP accepted and deployed; evidence is in document31.

## What this endpoint does

Cairn exposes a read-only `search_knowledge_base` tool over **Streamable HTTP** at `/mcp`, sharing
the same retrieval implementation and API-key permissions as `POST /v1/retrieval/query`. It is not a
chat-model API, does not generate answers and does not require a specific Agent framework.

The compatibility baseline is MCP protocol **2025-11-25**, using official Python SDK **1.30.0**.
Responses use JSON; optional standalone GET/SSE and DELETE sessions are not provided (HTTP405).
No MCP session ID or replica affinity is needed. Clients may initialize, discover tools and call
them; credentials must accompany every HTTP request. This is not legacy HTTP+SSE or a stdio server.

Only search is exposed in this increment. Historical M13 `list_knowledge_bases`, `get_document`,
resource browsing, automatic KB-name descriptions and full multi-client product certification
remain separate work. No Agent project is modified by this delivery.

## Credentials and endpoint

### Starting the service yourself

The Compose preset now runs a dedicated managed MCP service. The web page controls its selected
port and start/stop/restart/logs; see [MCP service management](06-mcp-service-management.md).
For an already initialized local installation, open Docker Desktop and run:

```powershell
cd D:\code\Cairn
docker compose --profile local-model up -d
docker compose ps
```

Both the web interface and MCP then use port8080; MCP URL is `http://127.0.0.1:8080/mcp`.
After pulling changed source, use `up -d --build` instead to update images. Keep the existing `.env`,
master key and volumes; do not initialize new credentials for an existing database.

The dedicated MCP URL is `http://127.0.0.1:<selected-port>/mcp` (default8081); the compatibility
URL remains port8080/mcp. Both obey the same Stop state. Do not create unmanaged duplicate data API
containers as a workaround: embedded MCP is deliberately disabled in Compose-managed mode.
PostgreSQL, Redis, the configured embedding service and maintain worker must remain available.

1. Start Cairn using the local product quickstart and index at least one supported document.
2. In **API keys**, issue a dedicated key with **`kb:query`**, scoped to the intended KBs. Copy it
   once and store it privately. The key cannot exceed its owner's access.
3. On this computer, use `http://127.0.0.1:8080/mcp` and the request header
   `Authorization: Bearer <your-cairn-api-key>`.
4. With exactly one authorized KB, the tool can infer it. For multiple KBs, pass its public
   `kb_...` ID explicitly. IDs are available from the knowledge-base page URL and the bounded tool
   input enum; names/document lists are not exposed by this initial tool.

Browser login cookies do not authenticate MCP, even on `CAIRN_ROLE=all`. Never place the bearer key
inside a tool argument, prompt, chat history or public frontend bundle. Use a trusted client that
supports static Authorization headers. OAuth discovery/interactive authorization is not implemented;
clients requiring OAuth-only setup are not automatically compatible with this API-key deployment.

Client configuration formats differ. The connection values below are authoritative, but this JSON
is an illustrative shape for clients with `mcpServers`/`type`/`headers`, not a universal client schema:

```json
{
  "mcpServers": {
    "cairn": {
      "type": "http",
      "url": "http://127.0.0.1:8080/mcp",
      "headers": { "Authorization": "Bearer <YOUR_PRIVATE_CAIRN_API_KEY>" }
    }
  }
}
```

## Verify without changing an Agent

From the Cairn repository in PowerShell:

```powershell
$env:CAIRN_API_KEY = "your-private-cairn-key"
uv run python scripts/mcp_smoke.py
uv run python scripts/mcp_smoke.py --query "A short question about your documents" --kb-id "kb_YOUR_ID"
Remove-Item Env:CAIRN_API_KEY
```

This uses the official MCP client SDK, not an ad-hoc HTTP imitation. It prints server/protocol/tool
names and hit counts, not secrets or retrieved document contents. Empty results are a successful
search with no evidence; they are not grounds for an Agent to fabricate an answer.

Tool arguments:

```json
{
  "query": "What is the return policy?",
  "knowledge_base_id": "kb_YOUR_ID",
  "top_k": 5,
  "max_context_tokens": 4000
}
```

`query` is required and must be concise enough for the chosen embedding model. `top_k` accepts1..20;
context budget100..20000 uses Cairn's retrieval token counts, not necessarily the client's chat-model
tokenizer. Results include readable numbered passages with KB/document/chunk IDs and structured
retrieval results, usage, degradation notices and partial failures. Unsupported filters/reranking/
parent expansion are not advertised; search uses hybrid retrieval without reranking/parent expansion.

## Network and security configuration

- Local default accepts Host `localhost`, `127.0.0.1`, `[::1]` with optional port, and requests
  **without Origin**. Present Origin values are denied unless explicitly allowed. No browser CORS
  support is implied by an Origin allowlist; server-to-server clients normally omit Origin.
- For a trusted remote HTTPS deployment, set the exact externally visible hosts/origins:

```dotenv
CAIRN_MCP__ALLOWED_HOSTS=["knowledge.example.com"]
CAIRN_MCP__ALLOWED_ORIGINS=["https://trusted-client.example.com"]
CAIRN_MCP__MAX_REQUEST_BYTES=65536
CAIRN_MCP__REQUEST_TIMEOUT_S=15
```

- Do not expose the loopback development preset publicly without TLS/network controls. `127.0.0.1`
  from another machine/container refers to that caller, not to Cairn. Configure the reachable address
  and matching Host allowlist rather than disabling DNS-rebinding protection.
- Search checks authorization on every tool execution; tool-list enums are guidance, not security.
  The underlying authz principal cache has a60-second TTL for ordinary grant changes. Key revocation
  explicitly invalidates the key cache; use it for immediate access removal. MCP maintains no extra
  per-key description cache that could extend staleness.
- Default request envelope limit64KiB; tool result limit512KiB. Oversized source metadata is rejected
  with `OUTPUT_TOO_LARGE`, not silently truncated. Retrieved text is untrusted data, not instructions.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| HTTP401 | Supply a valid unrevoked Cairn API key via Authorization, not a cookie |
| HTTP403/421 | Check key IP policy and configured Origin/Host; do not bypass the guard |
| HTTP404 | Use `/mcp` on data/all role; control-only instances intentionally do not mount it |
| HTTP405 on GET/DELETE | Expected for this stateless JSON implementation; use a Streamable HTTP client |
| HTTP406 | Client must accept application/json; normal MCP clients also advertise text/event-stream |
| HTTP413 | Request envelope exceeds configured bound; do not send whole documents/chat history |
| HTTP429 | Respect rate-limit/Retry-After headers |
| Tool isError=true | Read stable error code; fix KB scope, arguments, model readiness or retryable availability |

Protocol reference: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
and https://modelcontextprotocol.io/specification/2025-11-25/server/tools .
