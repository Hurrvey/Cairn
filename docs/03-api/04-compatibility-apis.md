# Compatibility APIs and SDKs

**Document:** `03-api/04-compatibility-apis.md`
**Status:** Normative
**Date:** 2026-08-28

Four surfaces that let existing tools use Cairn with **zero integration code**. Roughly two weeks
of work each, and together they are the highest-leverage adoption work in the plan: they move
the integration cost for a new user from "write a client" to "paste a URL and a key".

---

## 1. MCP server (`FR-J-*`, `NFR-C-03`) — the primary channel

**Endpoint:** `POST /mcp` (streamable HTTP)
**Auth:** `Authorization: Bearer cairn_sk_live_...`
**Module:** [M13](../02-modules/M13-mcp-server.md)

### Client setup — the entire integration

```jsonc
{
  "mcpServers": {
    "cairn": {
      "url": "https://cairn.example.com/mcp",
      "headers": { "Authorization": "Bearer cairn_sk_live_7Kq2..." }
    }
  }
}
```

That is the whole thing. The Agent now has knowledge retrieval.

### Tools

| Tool | Permission | Purpose |
| --- | --- | --- |
| `search_knowledge_base` | `kb:query` | Retrieve relevant passages with citations |
| `list_knowledge_bases` | `kb:query` or `kb:read` | Discover what is available |
| `get_document` | `kb:read` | Fetch full content for expansion |

### Dynamic descriptions (`FR-J-04`)

The tool description is generated per key from the KBs that key can actually reach:

```
Search the organisation's knowledge bases for relevant passages.
Use this whenever the user asks about internal documentation, policies,
product details, or anything that may be recorded in these sources.

Available knowledge bases:
- kb_01HQZX3N9K2M5P7R8T — Ops Handbook: Internal operations documentation
  and runbooks (412 documents, updated 2026-08-27)
- kb_02KMV8B4C6D9F1G3H5 — Product Specs: Current and historical product
  specifications (1,204 documents, updated 2026-08-28)
```

A static description gives the client model nothing to route with. This turns tool selection
from guesswork into a decision, and the `knowledge_base_id` enum is populated from the same list
so an inaccessible KB cannot be hallucinated.

---

## 2. Dify External Knowledge API (`NFR-C-01`)

**Endpoint:** `POST /retrieval`
**Module:** [M09](../02-modules/M09-retrieval.md)

Implements Dify's contract exactly. In Dify: *Knowledge → Connect to an External Knowledge
Base* → API endpoint `https://cairn.example.com`, API key, and the Cairn `kb_...` ID as the
Knowledge ID.

```jsonc
// request
{ "knowledge_id": "kb_01HQZX3N9K2M5P7R8T",
  "query": "How do I rotate the signing key?",
  "retrieval_setting": { "top_k": 5, "score_threshold": 0.35 } }

// response
{ "records": [
    { "content": "Key rotation is performed via…", "score": 0.873,
      "title": "Ops Handbook 2026",
      "metadata": { "path": "Security > Key Management", "page": 14,
                    "document_id": "doc_01HQ…", "chunk_id": "chk_01HQ…",
                    "source_url": "https://docs.internal/…" } } ] }
```

Error shape on this endpoint follows Dify's convention, not RFC 9457:

| `error_code` | HTTP | Meaning |
| --- | --- | --- |
| 1001 | 400 | Invalid Authorization header format |
| 1002 | 403 | Authorization failed |
| 2001 | 404 | Knowledge base not found |

Every Dify deployment becomes a potential Cairn user with no code written on either side.

---

## 3. LangChain and LlamaIndex retrievers (`NFR-C-02`)

Small published packages — about fifty lines each — that put Cairn inside the two ecosystems
where RAG clients already live.

```python
# pip install cairn-langchain
from cairn_langchain import CairnRetriever

retriever = CairnRetriever(
    base_url="https://cairn.example.com",
    api_key=os.environ["CAIRN_API_KEY"],
    knowledge_base_ids=["kb_01HQZX3N9K2M5P7R8T"],
    top_k=5,
    search_mode="hybrid",
)
chain = create_retrieval_chain(retriever, document_chain)
```

```python
# pip install cairn-llamaindex
from cairn_llamaindex import CairnRetriever

retriever = CairnRetriever(base_url=..., api_key=..., knowledge_base_ids=[...], top_k=5)
query_engine = RetrieverQueryEngine.from_args(retriever)
```

Both map Cairn hits to the framework's document type, preserving `score`, `metadata`, and
`source` so citation rendering works unchanged.

---

## 4. Official SDKs (`FR-I-09`)

October8 accepted local SDK increment: generated models/types with pinned non-writing drift
checks, handwritten bounded HTTP clients, built wheel/npm tarball and real Compose acceptance.
Dedicated SDK CI/nightly coverage and registry publishing remain separate work. Run generation
checks after changing OpenAPI; no claim that a remote SDK CI job is already enforcing this.

| Package | Language | Contents |
| --- | --- | --- |
| `cairn-client` (import `cairn_client`) | Python 3.11+ | Sync + async clients, generated models, core CRUD/query/batch/citations, optional retriever adapters |
| `@cairn/client` | TypeScript / Node 20+ | fetch client, generated types, core CRUD/query/batch/citations, AbortSignal |

Retries default to0. Opt-in retries honor Retry-After for eligible read/query operations;
mutations are never automatically replayed. Typed errors expose status,code,request ID and
retry delay with safe diagnostics. Python sync uses soft aggregate plus per-I/O timeouts;
Python async/TS enforce hard overall cancellation. Streaming clients, specialized exception
subclasses and broader rate-limit conveniences remain follow-ups.

```python
import os
from cairn_client import CairnClient, CairnError

with CairnClient("https://knowledge.example.com", os.environ["CAIRN_API_KEY"]) as client:
    try:
        result = client.retrieve({
            "targets": [{"knowledge_base_id": "kb_YOUR_ID"}], "query": "example"
        })
    except CairnError as exc:
        print(exc.status_code, exc.code, exc.request_id, exc.retry_after)
```

---

## 5. OpenAI-compatible embedding endpoint (P2, Phase 5)

`POST /v1/embeddings` matching OpenAI's schema, so tools that only speak that dialect can use
Cairn's configured embedding models. Cheap to add once the model gateway exists; low priority
because it does not serve the core retrieval use case.

---

## 6. Integration test matrix

Each surface has an end-to-end test against a **real client**, not a mock. A compatibility API
that only passes its own tests is not compatible.

| Surface | Client under test | Test |
| --- | --- | --- |
| MCP | Claude Code + one other MCP client | Configure, list tools, search, verify citations |
| Dify | Dify instance in Docker | Connect external KB, run a retrieval, verify records |
| LangChain | `langchain` current release | Build a chain, retrieve, verify document mapping |
| LlamaIndex | `llama-index` current release | Same |
| Python SDK | Generated package | Full CRUD + retrieval + error handling |
| TS SDK | Generated package | Same |

Run in CI nightly, since these break when the *other* side releases — which is exactly the
failure that a passing unit-test suite will not catch.
