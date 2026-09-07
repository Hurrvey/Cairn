# ADR-0002 — Control plane / data plane split

**Status:** Accepted · **Date:** 2026-08-28 · **Deciders:** Architecture

## Context

Cairn serves two workloads with almost nothing in common:

| | Data plane | Control plane |
| --- | --- | --- |
| Operations | `POST /v1/retrieval/query`, MCP search | CRUD, config, uploads, jobs, admin |
| Traffic | High RPS, machine clients, bursty | Low RPS, human clients |
| Latency | p95 < 150 ms, SLO-bound (`NFR-P-01`) | Seconds are acceptable |
| Consistency | Read-only, cache-tolerant | Transactional |
| Failure impact | Every Agent stops working | An admin waits |
| Scaling signal | RPS and p95 | Effectively none |

Running them in one process means a heavy admin export, a large `LIST documents`, or a
migration-triggered connection spike degrades retrieval — the one thing customers pay for.

## Decision

Split them into **two deployments of the same image**, selected by `CAIRN_ROLE`:

```python
ROUTERS = {
    "data":    [retrieval_router, mcp_router, discovery_router],
    "control": [auth_router, users_router, kb_router, ...],
    "all":     [...both...],          # dev and the `small` preset
}
```

Routing is by path at the ingress. Data-plane modules (`M09`, `M13`) MUST NOT import
control-plane modules — enforced by `import-linter` (`NFR-M-02`).

The data plane MUST NOT touch PostgreSQL on the happy path: principal resolution and KB config
come from Redis, content comes from the vector-store read model (ADR-0005).

## Consequences

**Positive**
- Control-plane failure does not affect retrieval (`NFR-R-02`).
- Independent autoscaling on the correct signal for each.
- Independent resource profiles — `api-data` gets more replicas and less memory each.
- Smaller data-plane attack surface: it exposes three routers, not thirty.
- Faster data-plane startup — no heavy control-plane imports.

**Negative**
- Two deployments to operate. Mitigated by `CAIRN_ROLE=all` for small installs.
- The import rule creates real friction — a data-plane developer who needs a KB field must ask
  for it in the cached DTO. **This friction is the point**; without it the boundary erodes in
  weeks.
- KB config reaching the data plane is eventually consistent (≤ 60 s). Accepted: config changes
  are rare and not safety-critical. Deletion is the exception and bumps the cache immediately.

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| One deployment, rely on async | A slow control-plane query still consumes connections, memory, and event-loop time |
| Two separate codebases | Duplicated auth, config, DTOs, tests. The one-image split gives isolation without duplication. |
| Read replicas only | Solves DB contention, not process contention or blast radius |
