# Cairn Documentation

**Documentation date:** 2026-08-28
**Audience:** Product, engineering, and coding Agents implementing Cairn.

---

## How to use these documents

Every document has a fixed role. The set is designed so that a developer or Agent can be
handed **one module spec** plus **three shared references** and produce correct, integrable
code without reading everything else.

### Required reading for every contributor (~45 min)

1. [`00-overview/01-product-brief.md`](00-overview/01-product-brief.md) — what we are building and, importantly, what we are not
2. [`00-overview/02-glossary.md`](00-overview/02-glossary.md) — the ubiquitous language; use these exact terms in code
3. [`01-architecture/01-system-architecture.md`](01-architecture/01-system-architecture.md) — the shape of the system
4. [`01-architecture/06-cross-cutting-conventions.md`](01-architecture/06-cross-cutting-conventions.md) — errors, IDs, config, logging, testing conventions

### Then: your assigned module spec

[`02-modules/Mxx-*.md`](02-modules/) — self-contained, implementation-ready. Each contains
its own domain model, public interface, owned tables, algorithms, errors, tests, and
acceptance criteria.

### Reference as needed

- [`01-architecture/03-data-model.md`](01-architecture/03-data-model.md) — the complete DDL, single source of truth for schema
- [`03-api/01-api-conventions.md`](03-api/01-api-conventions.md) — if you own HTTP endpoints
- [`05-quality/01-test-strategy.md`](05-quality/01-test-strategy.md) — before writing tests

---

## Full document index

### 00 — Product overview

| Doc | Contents |
| --- | --- |
| [01-product-brief.md](00-overview/01-product-brief.md) | Vision, problem, positioning, scope boundary, non-goals, success metrics, licensing |
| [02-glossary.md](00-overview/02-glossary.md) | Ubiquitous language — canonical terms used in code, DB, and API |
| [03-personas-and-journeys.md](00-overview/03-personas-and-journeys.md) | Personas, primary user journeys, end-to-end scenarios |
| [04-requirements.md](00-overview/04-requirements.md) | **Numbered FR/NFR catalog.** Every task and test traces to an ID here |

### 01 — Architecture

| Doc | Contents |
| --- | --- |
| [01-system-architecture.md](01-architecture/01-system-architecture.md) | Context, container, and component views; request paths; failure domains |
| [02-technology-stack.md](01-architecture/02-technology-stack.md) | Pinned choices with versions and rationale |
| [03-data-model.md](01-architecture/03-data-model.md) | Complete PostgreSQL DDL, indexes, partitioning, migration policy |
| [04-module-boundaries.md](01-architecture/04-module-boundaries.md) | Module map, dependency rules, ownership, enforcement |
| [05-adr/](01-architecture/05-adr/) | Architecture Decision Records ADR-0001 … ADR-0008 |
| [06-cross-cutting-conventions.md](01-architecture/06-cross-cutting-conventions.md) | IDs, errors, config, logging, tracing, time, i18n, code layout |

### 02 — Module specifications

| ID | Module | Plane | Phase |
| --- | --- | --- | --- |
| [M00](02-modules/M00-core.md) | Core foundation | shared | 0 |
| [M01](02-modules/M01-identity.md) | Identity — users, sessions, bootstrap | control | 0–1 |
| [M02](02-modules/M02-authz.md) | Authorization — grants, API keys, resolution | both | 1 |
| [M03](02-modules/M03-catalog.md) | Catalog — knowledge bases, documents, bindings | control | 2 |
| [M04](02-modules/M04-objectstore.md) | Object store drivers | shared | 2 |
| [M05](02-modules/M05-vectorstore.md) | Vector store drivers | shared | 2 |
| [M06](02-modules/M06-tasks.md) | Durable task queue | shared | 1 |
| [M07](02-modules/M07-ingestion.md) | Ingestion — sources, parsers, chunkers, enrichers | worker | 2–3 |
| [M08](02-modules/M08-embedding.md) | Embedding service | shared | 2 |
| [M09](02-modules/M09-retrieval.md) | **Retrieval** ★ | data | 2 |
| [M10](02-modules/M10-modelgw.md) | Model gateway | shared | 3 |
| [M11](02-modules/M11-pipelines.md) | Pipelines (Agent Workflow) | control+worker | 4 |
| [M12](02-modules/M12-functions.md) | Function library + sandbox | control+worker | 4 |
| [M13](02-modules/M13-mcp-server.md) | **MCP server** ★ | data | 3 |
| [M14](02-modules/M14-evaluation.md) | Evaluation & model testing | control | 5 |
| [M15](02-modules/M15-platform.md) | Platform — audit, settings, quotas | control | 1 |
| [M16](02-modules/M16-web-ui.md) | Web UI | frontend | 1–5 |

★ = data plane, latency-critical, SLO-bound.

### 03 — API

| Doc | Contents |
| --- | --- |
| [01-api-conventions.md](03-api/01-api-conventions.md) | Versioning, errors, pagination, idempotency, rate limits, deprecation policy |
| [02-data-plane-api.md](03-api/02-data-plane-api.md) | The retrieval contract — the product surface |
| [03-control-plane-api.md](03-api/03-control-plane-api.md) | Management endpoints, grouped by module |
| [04-compatibility-apis.md](03-api/04-compatibility-apis.md) | MCP, Dify External Knowledge, LangChain/LlamaIndex, SDKs |

### 04 — Plan

| Doc | Contents |
| --- | --- |
| [01-development-plan.md](04-plan/01-development-plan.md) | 6 phases, milestones, exit criteria, staffing, critical path |
| [02-work-breakdown.md](04-plan/02-work-breakdown.md) | **Full WBS: every task with ID, module, deps, estimate, acceptance** |
| [03-team-and-workflow.md](04-plan/03-team-and-workflow.md) | Streams, ownership, branching, PR rules, Definition of Done, **Agent protocol** |
| [04-risk-register.md](04-plan/04-risk-register.md) | Risks, likelihood/impact, mitigation, owner, trigger |
| [05-parser-spike.md](04-plan/05-parser-spike.md) | PDF candidate/licensing checkpoint; genuine 30-document quality gate still open |
| [06-continuation-verification.md](04-plan/06-continuation-verification.md) | Historical service-backed verification and continuation links |
| [07-embedding-implementation.md](04-plan/07-embedding-implementation.md) | Dense embedding/tokenizer implementation checkpoint |
| [08-chunking-implementation.md](04-plan/08-chunking-implementation.md) | Basic chunker rules, identity and source provenance |
| [09-worker-pipeline.md](04-plan/09-worker-pipeline.md) | Four-stage worker and ordered repair/acceptance evidence |
| [10-office-language.md](04-plan/10-office-language.md) | Office parser and language-resolution scope and limits |
| [11-advanced-chunking.md](04-plan/11-advanced-chunking.md) | Semantic/custom implementation and ordered safety validation |
| [13-ingestion-completion-coordination.md](04-plan/13-ingestion-completion-coordination.md) | Serial worker ownership and acceptance rules |
| [15-execution-ledger.md](04-plan/15-execution-ledger.md) | **Current restart point: step log, accepted scope, failures, resources and next action** |
| [16-lifecycle-acceptance-plan.md](04-plan/16-lifecycle-acceptance-plan.md) | Pending rebuild/re-embed/retry/retirement package and regression matrix |
| [17-version-snapshot-implementation.md](04-plan/17-version-snapshot-implementation.md) | Version-owned model/configuration snapshots, safe legacy migration and allocation evidence |
| [18-lifecycle-followup-review.md](04-plan/18-lifecycle-followup-review.md) | Historical source review; accepted fan-out is tracked in document19, later lifecycle gaps remain |
| [19-reindex-fanout-implementation.md](04-plan/19-reindex-fanout-implementation.md) | Resumable fan-out, activation/deletion barriers, replay and artifact-reuse evidence |
| [20-chunk-reembed-implementation.md](04-plan/20-chunk-reembed-implementation.md) | Manual chunk reembedding, edit-generation fencing, exact token/payload validation and retry evidence |

### 05 — Quality

| Doc | Contents |
| --- | --- |
| [01-test-strategy.md](05-quality/01-test-strategy.md) | Test pyramid, fixtures, coverage gates, golden datasets, CI |
| [02-performance-slo.md](05-quality/02-performance-slo.md) | SLIs, SLOs, benchmark harness, load profiles, capacity model |
| [03-security-baseline.md](05-quality/03-security-baseline.md) | Threat model, controls checklist, sandbox and SSRF requirements |

### 06 — Operations

| Doc | Contents |
| --- | --- |
| [01-deployment.md](06-ops/01-deployment.md) | Compose topology, sizing presets, Helm/K8s, config reference, upgrades |
| [02-observability-runbook.md](06-ops/02-observability-runbook.md) | Metrics, traces, dashboards, alerts, incident runbooks |

---

## Traceability chain

```
Requirement (FR-C-03)  →  Module spec (M03 §5.2)  →  WBS task (T-M03-07)
                                                   →  Test case (TC-M03-07-a)
                                                   →  Acceptance criterion
```

Every WBS task cites the requirement IDs it satisfies. Every module spec lists the
requirements it owns. Nothing is built that does not trace to a requirement; no requirement
is unassigned. This is what makes parallel assignment safe.

---

## Document conventions

- **MUST / SHOULD / MAY** are used in the RFC 2119 sense.
- Code identifiers, table names, and API fields are in `code font` and are **normative** —
  implement them exactly as written.
- `TODO(owner)` marks a deliberate open question. `OPEN QUESTION` sections list decisions
  that must be resolved before the owning task starts.
- Estimates are in **ideal engineer-days** for one competent implementer with the spec in hand.
- Documents are written in English. Terminology in [`02-glossary.md`](00-overview/02-glossary.md)
  is normative regardless of the language used in UI copy.
