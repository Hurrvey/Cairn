# ADR-0001 — Modular monolith rather than microservices

**Status:** Accepted · **Date:** 2026-08-28 · **Deciders:** Architecture

## Context

Cairn has ~17 identifiable modules spanning identity, ingestion, retrieval, and extensibility.
The team is 5–8 engineers. The system must support independent scaling of retrieval and
ingestion, and must remain operable by a single administrator on one node (`NFR-D-01`).

Microservices would give physical isolation and independent deploys, at the cost of network
boundaries, distributed transactions, service discovery, per-service CI/CD, and a distributed
tracing burden — before any product value exists.

## Decision

Build a **modular monolith**: one Python package `cairn` with statically enforced internal
boundaries, deployed as multiple *roles* of one image (`api-control`, `api-data`, `worker-*`).

Boundaries are enforced by `import-linter` in CI, not by convention
([module boundaries §4](../04-module-boundaries.md)).

## Consequences

**Positive**
- One build, one test suite, one deployment artifact, one migration history.
- Refactoring across module lines is a compiler-checked operation, not a coordinated release.
- Local development is `docker compose up`, not a service mesh.
- Scaling still works: roles are separate deployments with separate replica counts.

**Negative**
- A memory leak or crash in one module affects its whole process. Mitigated by role separation
  — the data plane cannot be crashed by control-plane code it does not import.
- Language choice is uniform. Accepted: Python is right for this domain.
- Discipline is required. Mitigated: CI enforcement makes the discipline automatic.

**Reversibility — high.** Because `service.py` facades are the only cross-module surface and
data-plane modules import nothing from the control plane, extracting `M09 retrieval` into its
own service is: containerize, replace the facade calls with HTTP, done. The boundary work is
already paid for.

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Microservices from day one | Distributed-systems tax before product-market fit; team too small |
| Single-process, no enforced modules | Predictable outcome at ~30k lines: nothing can be scaled or extracted |
| Serverless functions | Cold starts fatal to a p95 < 150 ms SLO; long-running parse tasks do not fit |
