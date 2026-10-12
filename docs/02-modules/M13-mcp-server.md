# M13 — MCP Server ★

**Implementation checkpoint (2026-09-17):** the user approved a narrower first increment:
generic read-only `search_knowledge_base` over Streamable HTTP, reusing existing retrieval/authz.
See `../04-plan/31-generic-mcp-implementation.md` and `../06-ops/05-mcp-quickstart.md` for actual
implemented scope and acceptance. The three-tool/resource design and all15 acceptance cases below
remain the broader target, not a claim that list/get/resources/OAuth or every MCP client is supported.

| | |
| --- | --- |
| **Package** | `cairn.mcpserver` |
| **Layer** | L3 **data plane** |
| **Phase** | 3 |
| **Owner** | Retrieval owner (same as M09) |
| **Depends on** | M00, `M02.dataplane`, M09 |
| **Forbidden imports** | every control-plane module (CI-enforced) |
| **Tables owned** | none |
| **Requirements owned** | FR-J-01..05, FR-J-07, NFR-C-03 |

---

## 1. Purpose and scope

Expose Cairn knowledge bases as MCP tools so any MCP client — Claude Code, Claude Desktop, and
every other MCP-capable Agent — can use them **with zero integration code**.

> Given the product thesis is "Agents are clients of this platform", this is arguably the
> **primary** interface, not a compatibility shim. A user pastes a URL and a key into their MCP
> config and their Agent gains knowledge retrieval. Nothing else in the plan has a lower
> integration cost or a higher adoption impact for the effort.

**In scope:** the MCP server over streamable HTTP, tool definitions, dynamic tool descriptions,
authentication, citation formatting, optional resource browsing.

**Out of scope:** MCP *client* capability (M11 owns that for enrichment nodes), agent
orchestration.

---

## 2. Transport and protocol

- **Streamable HTTP** at `POST /mcp` (plus `GET /mcp` for the SSE stream where required).
- Conforms to the current MCP specification (`NFR-C-03`).
- Authentication: `Authorization: Bearer cairn_sk_...` — the same API key mechanism, the same
  permissions (`FR-J-03`). No separate credential type.
- Stateless per request where the protocol permits; session state, when required, lives in Redis
  keyed by the MCP session ID so any `api-data` replica can serve any request.

---

## 3. Tools (`FR-J-02`)

### `search_knowledge_base`

```jsonc
{
  "name": "search_knowledge_base",
  "description": "<DYNAMICALLY GENERATED — see §4.1>",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query":               { "type": "string", "description": "Natural-language search query." },
      "knowledge_base_id":   { "type": "string", "description": "Which knowledge base to search.",
                               "enum": ["kb_01H…", "kb_02K…"] },
      "top_k":               { "type": "integer", "default": 5, "minimum": 1, "maximum": 20 },
      "max_context_tokens":  { "type": "integer", "default": 4000 },
      "filters":             { "type": "object", "description": "Optional metadata filters." }
    },
    "required": ["query"]
  }
}
```

Response content is formatted for a model to read, with citations a client can render:

```
Found 3 relevant passages in "Ops Handbook".

[1] Security > Key Management (page 14)
Key rotation is performed via the `cairn-admin rotate-key` command…
    source: kb_01H… / doc_01H… / chk_01H…   relevance: 0.87

[2] …
```

`structuredContent` carries the machine-readable form (the same `RetrievalHit` objects as the
HTTP API) so clients can render citations properly rather than parsing prose.

### `list_knowledge_bases`

Returns the KBs the calling key can access, with name, description, document count, and last
update — so the model can choose a target itself.

### `get_document`

Fetches full document content by ID for expansion after a search. Requires `kb:read`, not just
`kb:query` — this is exactly the distinction `FR-B-07` exists for, and it means a query-only key
gets search without bulk read.

---

## 4. Behaviour

### 4.1 Dynamic tool descriptions (`FR-J-04`) — the highest-value detail in this module

A static description ("Search a knowledge base") gives the client model nothing to route with.
Generating the description from the key's actual accessible KBs turns tool selection from
guesswork into a decision:

```python
async def _describe_search_tool(self, principal: Principal) -> str:
    kbs = await self.retrieval.list_accessible_kbs(principal)
    lines = [
        "Search the organisation's knowledge bases for relevant passages.",
        "Use this whenever the user asks about internal documentation, policies, "
        "product details, or anything that may be recorded in these sources.",
        "",
        "Available knowledge bases:",
    ]
    for kb in kbs:
        lines.append(f"- {kb.id} — {kb.name}: {kb.description or 'no description'} "
                     f"({kb.doc_count} documents, updated {kb.last_indexed_at:%Y-%m-%d})")
    if len(kbs) == 1:
        lines.append("\nknowledge_base_id may be omitted; there is only one.")
    return "\n".join(lines)
```

The `enum` on `knowledge_base_id` is populated from the same list, so a client model cannot
hallucinate an inaccessible KB ID.

Descriptions are cached per `(key_hash, perm_version)` for 60 s.

### 4.2 Authorization

Identical to M09: `kb:query` for search, `kb:read` for `get_document` and full listing. A key
with access to zero KBs receives a valid tool list with an empty enum and a description saying
so — never a 403 at the protocol level, which most clients surface as a broken server rather
than as an empty capability.

### 4.3 Errors

MCP tool errors are returned as tool results with `isError: true` and a readable message, not as
transport-level failures — a client model can then adapt (retry with a different KB, tell the
user) instead of the whole server appearing broken.

Protocol-level errors (bad auth, malformed JSON-RPC) use the MCP error format.

### 4.4 Resources (`FR-J-07`, P2)

Expose KBs as an MCP resource tree — `cairn://kb/{kb_id}/documents/{doc_id}` — so clients can
browse rather than only search.

---

## 5. Performance requirements

| Operation | Budget |
| --- | --- |
| `tools/list` | < 30 ms p95 (cached descriptions) |
| `search_knowledge_base` | Same SLO as M09: p95 < 400 ms with rerank |
| `initialize` handshake | < 50 ms |

Inherits the entire M09 latency budget — this is the same hot path with a different envelope.

---

## 6. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M13-01 | `initialize` handshake conforms to the MCP spec | NFR-C-03 |
| TC-M13-02 | `tools/list` returns all three tools with valid JSON Schema | FR-J-02 |
| TC-M13-03 | **Description lists exactly the KBs the key can access** | FR-J-04 |
| TC-M13-04 | **`knowledge_base_id` enum excludes inaccessible KBs** | FR-J-04 |
| TC-M13-05 | Search returns formatted content plus `structuredContent` | FR-J-05 |
| TC-M13-06 | Citations resolve to real chunk, document, and KB IDs | FR-J-05 |
| TC-M13-07 | `kb:query`-only key can search but not `get_document` | FR-B-07 |
| TC-M13-08 | Invalid API key returns an MCP protocol auth error | FR-J-03 |
| TC-M13-09 | A key with zero KBs gets an empty enum, not a 403 | §4.2 |
| TC-M13-10 | Tool errors return `isError: true`, not a transport failure | §4.3 |
| TC-M13-11 | Session state works across `api-data` replicas | §2 |
| TC-M13-12 | Rate limits apply and are reported | FR-I-07 |
| TC-M13-13 | **`import-linter`: no control-plane import** | NFR-M-02 |
| TC-M13-14 | **End-to-end against a real MCP client SDK** | NFR-C-03 |
| TC-M13-15 | Description cache invalidates on `perm_version` bump | §4.1 |

Coverage target: **85%**.

---

## 7. Acceptance criteria

- [ ] All 15 test cases pass
- [ ] **Verified working in Claude Code and one other MCP client** by copy-pasting a config
- [ ] Journey J5 completes with zero integration code
- [ ] Setup documentation is a single copy-pasteable JSON block
- [ ] `import-linter` confirms data-plane isolation

---

## 8. Task breakdown

| Task | Description | Est (d) |
| --- | --- | --- |
| T-M13-01 | MCP protocol layer: JSON-RPC, streamable HTTP, initialize | 2.0 |
| T-M13-02 | Auth integration via `M02.dataplane` | 0.5 |
| T-M13-03 | **Dynamic tool description generation + cache** | 1.5 |
| T-M13-04 | `search_knowledge_base` tool | 1.0 |
| T-M13-05 | `list_knowledge_bases` tool | 0.5 |
| T-M13-06 | `get_document` tool | 0.5 |
| T-M13-07 | Content formatting + citations + `structuredContent` | 1.0 |
| T-M13-08 | Error mapping | 0.5 |
| T-M13-09 | Session state in Redis | 0.5 |
| T-M13-10 | MCP resources (P2) | 1.5 |
| T-M13-11 | Client-compatibility testing + setup docs | 1.5 |
| T-M13-12 | Tests TC-M13-01..15 | 1.5 |
| | **Total** | **12.5** |
