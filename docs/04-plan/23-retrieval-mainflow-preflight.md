# Retrieval main-flow preflight

Date: 2026-09-14. Status: HISTORICAL SOURCE INVESTIGATION.
September15: bounded first retrieval API implemented and accepted in document24/ledger L113.
The source findings below describe the earlier checkpoint. Advanced retrieval, runtime refresh,
general purge/retirement, broader permission audits and full UI/deployment acceptance remain open.
Coordinator prepared this while the sole worker implements bounded stage recovery (document22).
This supports document21's runnable-product priority; it does not add a second implementation agent.

## Existing seams and gaps

- M09 specification defines authorized query -> runtime snapshot -> embedding -> search -> cited
  result as the product path. No retrieval package/router currently exists.
- `KnowledgeBaseRuntime`, `BindingRefModel` and `RetrievalConfig` currently live under catalog.
  M09 forbids catalog imports. Move credential-free shared runtime/config DTOs into an appropriate
  shared module with compatibility exports and serialization tests before mounting M09; do not
  weaken the intended data-plane boundary merely to reuse a convenient import.
- EmbeddingService, TEI/Infinity providers and ModelRef are available. PipelineRuntime is NOT a
  retrieval resolver: it imports ingestion/catalog/modelgw. Data-plane model endpoint/tokenizer
  provisioning needs a public configuration seam, with pinned active-model identity preserved and
  credentials kept out of public runtime payloads.
- `authz.dataplane` provides API-key resolution; `Principal.can` supports in-memory permission
  decisions. Existing authz.deps imports platform audit, and existing session middleware imports
  identity. Audit actual transitive imports and initialization when adding a data-only app path.
- DataPlaneAuthz's IP allowlist method currently returns on unavailable/missing Redis data. Assess
  and test fail-closed behavior for restricted keys before exposing retrieval; this is a concrete
  security preflight finding, not proof of an accepted public endpoint.
- VectorStore supports dense queries, lexical query text, filters and denormalized hit payloads.
  pgvector performs SQL by definition; distinguish vector-driver DB access from forbidden catalog
  hydration/authorization DB calls when instrumenting the data-plane cache-hit path.
- Parent chunks deliberately have no vectors. Parent expansion cannot magically fetch them from
  the current vector index. Provide a published read model/cache or explicitly unsupported feature
  response; never reach into catalog ORM from retrieval. Do not claim parent expansion works merely
  because child payloads contain parent IDs.
- Current test queries call vector drivers directly, not a routed authentication/authorization
  chain. They are model/index evidence, not M09 endpoint evidence.
- Recovery review found nested document routes can authorize path KB while passing only document
  ID to a service. The retry endpoint is being corrected in document22. Audit get/delete and other
  nested routes before the browser release: same-workspace tenant checks alone do not prove access
  to the document's actual KB. This source-level finding is not a claim that the whole audit is done.

## Bounded first endpoint acceptance

1. Match the documented request/response path and API conventions. Authenticate session/API key
   as appropriate; authorize every requested KB before resolving model or searching storage.
2. Read only coherent active-version runtime metadata. Handle unavailable/deleting/archived KBs
   explicitly; never query a building namespace or use desired model settings with old vectors.
3. Execute supported vector/fulltext/hybrid behavior using existing drivers and model adapters.
   Reject unsupported rerank/parent/custom modes clearly rather than silently ignoring options.
4. Return exact chunk text with document/chunk identifiers, source/page/bbox citations when known,
   scores and declared mode. Bound request sizes, candidate counts, context tokens and timeouts.
5. Verify authenticated ASGI/HTTP behavior with real PG/Redis/TEI: ingest supported docs through
   existing pipeline, query using an actual granted API key, deny other KB/workspace access,
   query after rebuild/manual edit, and prove no accidental control-plane ORM hydration.
6. Mount on the correct API role and activate data-plane import contracts for existing modules
   only. Route authorization enumeration must see the new dependency; do not mark retrieval public.

These are investigation/acceptance requirements, not finalized interface decisions. Design and
minimal release scope must be recorded before implementation. Recovery, deletion/retirement and
runtime ordering work remain bounded prerequisites; PDF/OCR is not required for this supported-format
first release and must remain visibly unavailable until actually implemented.
