# Risk Register

**Document:** `04-plan/04-risk-register.md`
**Status:** Living — reviewed weekly
**Date:** 2026-08-28

**Scoring:** Likelihood (L) and Impact (I) on 1–5. Score = L × I.
🔴 ≥ 15 critical · 🟠 9–14 high · 🟡 4–8 medium · 🟢 ≤ 3 low

---

## Top risks

### 🔴 RISK-01 — Parsing quality silently caps retrieval quality (L4 × I5 = 20)

**Description.** Naive PDF extraction interleaves multi-column text and destroys tables. The
information is lost at ingest and **no amount of retrieval tuning recovers it**. Teams routinely
spend months tuning HNSW parameters while their content was scrambled from day one.

**Why it scores highest.** It is invisible. Retrieval returns plausible-looking results;
quality is just quietly bad; and the causal chain from "bad answer" back to "parser produced
word soup on page 14" is long and nobody walks it.

**Mitigation**
- `T-M07-01` blocking spike in W5 evaluating Docling, MinerU, pypdfium2 on a fixed 30-document
  corpus scored on table-structure F1 and reading-order correctness
- Reference corpus with committed snapshot tests (`T-M07-14`) — parser regressions fail CI
- Chunk browser in the UI so a human can *look* at what was extracted (`T-M16-13`)
- Golden-set evaluation from Phase 5 makes quality measurable rather than assumed

**Owner:** Ingestion stream · **Trigger:** spike scores below 0.85 table F1 on any candidate

---

### 🔴 RISK-02 — Function sandbox is inadequate; RCE ships (L3 × I5 = 15)

**Description.** The Function Library is remote code execution by design. Interpreter-level
restriction (`RestrictedPython`, AST allowlists, `__builtins__` stripping) has published
bypasses for every variant. If the kernel boundary is not real, the platform is compromised.

**Mitigation**
- gVisor or Firecracker mandated by `FR-M-03`; no fallback to plain containers
- No network egress by default; sandbox network-segmented from Postgres, Redis, object and
  vector stores
- Escape-attempt suite `TC-M12-04..15` runs in CI on every PR touching M12
- **Hard gate:** independent security review sign-off on `T-M12-13` before merge
- **Documented fallback:** ship Phase 4 pipelines *without* the Function Library

**Owner:** Extensibility stream + Security reviewer · **Trigger:** any escape test fails, or
gVisor is unavailable in the target environment

---

### 🟠 RISK-03 — SSRF via crawler or pipeline HTTP nodes (L3 × I4 = 12)

**Description.** Crawl seeds and webhook URLs are attacker-controlled by definition. A seed of
`http://169.254.169.254/latest/meta-data/iam/security-credentials/` reads the deployment's cloud
credentials. Pre-flight IP checks are defeated by DNS rebinding.

**Mitigation**
- All outbound traffic through `core.http.safe_client()` (`T-M00-07`) — own DNS resolution, IP
  pinning to the validated address, revalidation on every redirect hop
- Tests `TC-M00-05..08`, `TC-M07-19/20` including the rebinding and redirect cases
- Crawl workers deployed in a network segment with no route to internal services (defence in
  depth — one missed code path should not be fatal)

**Owner:** Foundation + Ingestion · **Trigger:** any new outbound-request code path not using
`safe_client()`

---

### 🟠 RISK-04 — Retrieval SLO not met under real load (L3 × I4 = 12)

**Description.** p95 < 150 ms is a published contract. Missing it undermines the entire
positioning as infrastructure.

**Mitigation**
- Per-stage latency budgets defined in [M09 §6](../02-modules/M09-retrieval.md), each with its
  own histogram — a regression is attributable without profiling
- Load test `TC-M09-29` in CI from Phase 2, not as a Phase 5 discovery
- Postgres removed from the hot path (ADR-0002, ADR-0005) — one whole dependency out of the
  latency chain
- Embedding cache (`FR-G-02`) removes provider latency on 30–60% of queries
- Load shedding degrades rerank before availability, and says so

**Owner:** Retrieval stream · **Trigger:** p95 > 120 ms in any CI load run

---

### 🟠 RISK-05 — Phase 2 overruns and the product slips (L3 × I4 = 12)

**Description.** Phase 2 is 110% subscribed and contains the entire critical path. Every
downstream phase depends on it.

**Mitigation**
- Critical path identified and staffed with the strongest engineers
- Interface freezes at W4–W6 unblock parallel work
- 31% overall buffer, concentrated in Phases 4–5
- Documented cut list ordered in advance, so cutting is a decision not a scramble
- **Never cut tests from Phase 2** — retrieval regressions are silent, and a fast wrong product
  is worse than a late right one

**Owner:** Engineering lead · **Trigger:** > 3 days behind at the end of W7

---

### 🟠 RISK-06 — Module boundaries erode; the data plane becomes inseparable (L3 × I3 = 9)

**Description.** Someone imports `cairn.catalog.models.KnowledgeBase` into M09 "for just one
field". Six months later the data plane cannot be scaled, deployed, or extracted independently
and nobody remembers why it matters.

**Mitigation**
- `import-linter` in CI and pre-commit (`NFR-M-01/02`) — mechanical, not cultural
- `KnowledgeBaseRuntime` DTO is the *only* KB representation the data plane sees, and adding a
  field to it requires the M09 owner's sign-off — friction by design
- `TC-M09-28` asserts no Postgres connection on the retrieval happy path
- Boundary check is an explicit review item

**Owner:** Engineering lead · **Trigger:** any `import-linter` suppression

---

### 🟠 RISK-07 — Vector store drivers diverge in filter semantics (L3 × I3 = 9)

**Description.** pgvector and Qdrant translate the same filter differently — `$in` on an array,
`$ne` on a missing field, null handling. Users get **different results depending on backend**,
which is a correctness bug that no test the user writes will catch.

**Mitigation**
- Driver-neutral filter AST with **normative semantics documented** ([M05 §5](../02-modules/M05-vectorstore.md))
- Shared conformance suite asserting identical result sets across all drivers, including the
  edge cases (`TC-M05-04..08`)
- A driver is not "done" until it passes the suite

**Owner:** Knowledge stream · **Trigger:** any conformance divergence

---

### 🟡 RISK-08 — Embedding provider cost or rate limits throttle ingestion (L4 × I2 = 8)

**Mitigation:** embedding cache; local TEI/Infinity as the recommended production path; per-
provider concurrency limits; cost estimate shown before any reindex (`FR-G-08`); monthly token
quota with resumable pause rather than failure (`FR-O-04/05`).

**Owner:** Ingestion stream

---

### 🟡 RISK-09 — CJK retrieval quality is poor (L3 × I3 = 9)

**Description.** Chinese has no whitespace word boundaries. Default Postgres FTS is unusable for
it; `len(text.split())` under-counts tokens by ~100×, turning "512-token chunks" into chunks the
provider rejects; sentence splitting on `.` misses `。`.

**Mitigation:** `zhparser`/`pg_jieba` decided **before** the FTS schema migration; jieba for BM25
terms; model-native tokenizers for counting (`FR-F-11`); CJK documents in the reference corpus;
`TC-M07-06`, `TC-M08-09`.

**Owner:** Ingestion stream · **Trigger:** CJK recall below 80% of English on the golden set

---

### 🟡 RISK-10 — MCP specification changes (L3 × I2 = 6)

**Mitigation:** protocol layer isolated in `M13`; nightly compatibility tests against **real
clients** (`T-OPS-12`), which is where spec drift actually surfaces; version negotiation in the
handshake.

**Owner:** Retrieval stream

---

### 🟠 RISK-11 — AGPL contamination from parsing libraries (L3 × I4 = 12)

**Description.** PyMuPDF's AGPL/commercial licensing and version-dependent MinerU terms
require review before distribution with Apache-2.0 Cairn. The MinerU snapshot inspected on
2026-09-07 uses Apache-2.0 plus commercial-restricted additional terms, not plain AGPL;
see the [version-pinned checkpoint](05-parser-spike.md). A sidecar boundary does not
automatically discharge licence obligations. Dependencies and model weights also need review.

**Mitigation**
- `T-M07-01` spike explicitly scores licence risk, with **Docling (MIT)** and **pypdfium2
  (permissive)** as the presumed default for exactly this reason
- If an AGPL parser is required for quality: isolate it in a **separately distributed sidecar
  container** communicating over HTTP, never imported in-process
- Licence scanning in CI (`T-OPS-02`) fails the build on GPL/AGPL in the application image
- MinIO (AGPL) is already handled this way — separate unmodified container, with S3 documented
  as the alternative

**Owner:** Platform lead · **Trigger:** spike concludes only an AGPL parser meets quality bar ·
**Decision required by:** W5 (Milestone M2)

---

### 🟡 RISK-12 — Task queue throughput ceiling (L2 × I3 = 6)

**Description.** The Postgres-backed queue (ADR-0003) tops out around 5–10k claims/s.

**Assessment:** document ingestion peaks near 100 tasks/s — roughly **50× headroom**. The risk
is low but named so it is monitored rather than assumed.

**Mitigation:** `cairn_task_queue_depth` and `cairn_task_oldest_ready_age_seconds` monitored;
benchmark `TC-M06-19`; partial indexes keep the hot set small; documented escape hatch is
per-queue table partitioning, then a broker for the highest-volume queue only.

**Owner:** Foundation stream · **Trigger:** sustained claim latency > 50 ms

---

### 🟡 RISK-13 — Read-model drift between Postgres and the vector store (L2 × I3 = 6)

**Mitigation:** only `M07`'s index stage writes the read model; idempotent upserts keyed on
`chunk_id`; nightly reconciliation emitting `cairn_readmodel_drift_total` and re-enqueuing
repairs; **the read model is always disposable** — a rebuild is the blue/green flow (ADR-0007).

**Owner:** Ingestion stream · **Trigger:** drift metric non-zero

---

### 🟡 RISK-14 — Scope creep back toward an agent runtime (L3 × I2 = 6)

**Description.** "Can we add a chat node?" is a reasonable-sounding request that reopens
ADR-0008 and adds months.

**Mitigation:** ADR-0008 records the decision and its rationale; the product brief gives the
team a decidable rule (*does this make knowledge better, faster, safer, or easier to retrieve?*);
reversal requires a superseding ADR, not a sprint decision.

**Owner:** Product

---

### 🟢 RISK-15 — Key personnel loss (L2 × I3 = 6)

**Mitigation:** this documentation set exists precisely so a module is transferable; module
specs are implementation-complete; no module has fewer than two people familiar with it after
Phase 1; ADRs record *why*, which is the part that walks out the door with a person.

**Owner:** Engineering lead

---

## Summary

| ID | Risk | Score | Owner |
| --- | --- | --- | --- |
| RISK-01 | Parsing quality caps retrieval | 🔴 20 | Ingestion |
| RISK-02 | Inadequate sandbox → RCE | 🔴 15 | Extensibility + Security |
| RISK-03 | SSRF via crawler / HTTP nodes | 🟠 12 | Foundation + Ingestion |
| RISK-04 | Retrieval SLO missed | 🟠 12 | Retrieval |
| RISK-05 | Phase 2 overrun | 🟠 12 | Eng lead |
| RISK-11 | AGPL contamination | 🟠 12 | Platform lead |
| RISK-06 | Boundary erosion | 🟠 9 | Eng lead |
| RISK-07 | Driver semantic divergence | 🟠 9 | Knowledge |
| RISK-09 | Poor CJK quality | 🟡 9 | Ingestion |
| RISK-08 | Embedding cost / rate limits | 🟡 8 | Ingestion |
| RISK-10 | MCP spec drift | 🟡 6 | Retrieval |
| RISK-12 | Queue throughput ceiling | 🟡 6 | Foundation |
| RISK-13 | Read-model drift | 🟡 6 | Ingestion |
| RISK-14 | Scope creep to agent runtime | 🟡 6 | Product |
| RISK-15 | Key personnel loss | 🟢 6 | Eng lead |

## Review process

Weekly, 15 minutes: re-score, check triggers, add new risks, close resolved ones. A risk is
closed only when its mitigation is **implemented and verified**, not when it feels less scary.

Phase gates include a risk review. A 🔴 risk with an unfired mitigation blocks the gate.
