# M08 dense embedding increment

**Date:** 2026-09-07 · **Status:** in progress.

Execute T-M08-01..06 and T-M08-08..10 from the existing WBS. T-M08-07
(model sparse/BM25 statistics) and GPU throughput benchmarks remain separate.
The PDF spike is not a prerequisite of M08.

1. Define vectors, failures, provider protocol and model-tokenizer registry.
   Preserve the existing `modelgw.dto.ModelRef` import path but move its pure
   definition into core so importing embedding never initializes modelgw/SQLAlchemy.
2. Implement NFKC/whitespace preparation, exact tokenizer counting and truncation,
   float32 cache encoding, cache corruption recovery, normalization and validation.
3. Implement query and document embedding with ordered partial-cache merging,
   duplicate-input coalescing, provider batch limits and per-provider semaphores.
4. Implement deadlines, bounded retries/Retry-After and circuit breaking.
5. Add explicit TEI/Infinity bindings with SSRF-safe transport, streaming response
   caps and strict result-index validation. No automatic provider/model guessing,
   model downloads or unguarded private-network access.
6. Run targeted tests, real-Redis checks, full backend coverage and static gates;
   update this record and WBS status with results and limitations.

Tokenizers are prepared explicitly at startup: a local Hugging Face tokenizer.json
or a named tiktoken encoding. Unknown tokenizers fail; whitespace counting is not
a fallback. Model references must declare dimension, input limit and tokenizer id.
Local server bindings are configuration supplied by composition code, not database
reads on the embedding hot path. URLs/credentials are not carried in ModelRef.

Cache keys include model/config identity, provider namespace, tokenizer fingerprint,
purpose and the exact prepared/truncated input. This is stronger than the illustrative
M08 pseudocode: empty prefixes must not let query/document results collide, and
changing normalization or tokenizer settings must not reuse stale embeddings.

The initial clients expose no public HTTP route. Upload-worker/retrieval wiring,
provider management endpoints, hosted-model adapters and sparse embedding are not
claimed by this increment. A real GPU deployment and matching model assets are
required before claiming the performance and tokenizer-parity acceptance gates.
