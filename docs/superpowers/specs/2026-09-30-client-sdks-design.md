# Client SDKs and retriever adapters

## October8 timeout review resolution

The persistent reviewer approved a scoped distinction: Python sync uses a **soft aggregate
deadline plus per-I/O socket timeouts**, checked before attempts and after received chunks.
A real network read started near the aggregate limit can remain blocked for one configured
read timeout beyond that limit; this is not limited to custom injected transports. Never start
a new retry after the aggregate budget expires. Do not describe sync as a hard total deadline.
Python async and TypeScript retain hard overall cancellation across network and body reading.

Add a real loopback slow-drip-then-stall test with measured bounds and response close evidence.
Document the distinction in package README and public timeout docstrings. An async bridge for
the sync client is not required: it would change injected httpx.Client ownership and caller
thread/event-loop semantics. No background thread is allowed to continue hidden work after
claiming hard cancellation.

Approved direction: persistent design advisor. Implement outside backend packages while the
gateway full regression runs. Public package publication is not part of local development.

## Client contract

October8 compatibility decision, approved by the same reviewer: this scoped SDK contract
supersedes the old SDK examples in `docs/03-api/04-compatibility-apis.md` section4 for package
name (`cairn-client`, import `cairn_client`), minimum Python3.11/Node20, default retry0 and
the `CairnError` status/code/request-ID/retry-after interface. Streaming clients, specialized
exception subclasses, broader rate-limit header convenience APIs and nightly upstream-client
coverage are not delivered by this increment and remain explicit follow-ups.

Core acceptance requires runnable non-mutating generation checks and local package/test gates.
Dedicated CI wiring can follow separately; local checks are not evidence of CI coverage.
The repository globally ignores `tests/` and `docs/`: preserve that policy and record which
delivery files are actually tracked. Never assume ignored local tests will exist in a fresh
checkout, and do not silently change ignore rules or force-add files.

October8 generated-TypeScript resolution: request requiredness follows OpenAPI `required`.
Set the verified generator option `defaultNonNullable:false` rather than forcing callers to
send every server-default field (weight/strict/etc.). Regenerate; never hand-edit schema output
or change the server for this issue. Compile the exact minimal README example from an installed
npm tarball, including batch input, as a consumer contract test.

Standalone Python (sync/async httpx) and TypeScript (fetch) clients target the unified nginx URL.
Discovery/citations are control-plane endpoints; a bare api-data URL serves only query/batch.
No implicit origin switching, redirects, environment proxy use in Python, or API-key interpolation
into errors/repr. Base URLs must be http(s), with no query/fragment/userinfo. URL path segments are
encoded, never treated as arbitrary absolute URLs. API keys remain caller-owned secrets.

Generate model/schema types from the checked-in OpenAPI contract, not ORM code. Expose retrieve,
batch, knowledge-base list/get/create/update/delete, document upload/list/get/delete/retry,
chunk list/edit/get and content resolution. A low-level same-origin request method covers other
admin endpoints; do not imply machine keys have admin permissions. All HTTP failures use typed
status/code/request_id/retry_after errors; omit raw upstream HTML/bodies and request headers.
Default retries=0; no mutation retry. Batch partial_failures remain successful typed responses.
Python context managers close owned clients; injected clients remain caller-owned. TS takes
AbortSignal and bounds non-stream response size. Document content reads are bounded.

## Distribution and adapters

Adapter policy approved October8: default strict=True for target failure handling, but degradation
still requires an explicit bounded CairnRetrievalWarning even for zero results. With opt-in
strict=False, retain partial failures/notices on every returned framework document and warn;
all-target failures remain SDK errors. Final October8 review tightens diagnostics: warnings use
bounded fixed known stage/reason/code values (unknown becomes other), omit raw request IDs and
free-text details. Adapter-created exceptions also omit untrusted response request IDs. Structured
notice/failure metadata omits free-text detail, preserving state without automatically forwarding
arbitrary diagnostics into framework tracing. Normal result text and user metadata remain result
data and can include URLs; adapters never fetch those URLs. Do not globally change warning filters.
Actual callback tests must cover a server echoing alphanumeric key text into diagnostic fields;
character filtering alone does not protect secrets. Framework serialization,
repr/dump/tracing must not expose the core client or key as public model fields; use private
attributes/from_client construction. Never fetch returned source/citation URLs automatically.
Cancellation tests wait until upstream response reading has entered, then cancel and assert
response closure; cancellation before any request starts does not prove transport cleanup.

Build Python wheel and npm tarball locally, no registry publish. README examples read secrets
from environment. Python extras provide LangChain BaseRetriever and LlamaIndex BaseRetriever
adapters, preserving score/citation/degraded metadata and not dropping failures silently. Tests
use local HTTP transports and the actual optional base classes when installed. Root server does
not depend on the SDKs or adapter libraries.

## Acceptance

Framework primary references rechecked October8:
[LangChain BaseRetriever source](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/retrievers.py)
and [LlamaIndex BaseRetriever source](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/base/base_retriever.py).
Use the SDK's isolated installed lock versions for executable compatibility tests (currently
langchain-core1.6.6/llama-index-core0.14.25), not assumptions about main branches. LangChain's
native async override and LlamaIndex's `_aretrieve(QueryBundle)` must use AsyncCairnClient.

Both SDKs: auth headers, query/batch, partial failure preservation, citation bounds, special ID
encoding, error redaction, deadline/cancel/close behavior, package build and import. Run against
the isolated Compose nginx entrypoint on a real indexed KB and compare returned IDs. Generation
is deterministic and source-schema drift is checked. Optional adapter import/contract tests are
distinct from a live commercial application integration.
