# Development Plan

**Document:** `04-plan/01-development-plan.md`
**Status:** Baseline
**Date:** 2026-08-28
**Owner:** Product + Engineering lead

Weeks are numbered **W1–W26** relative to kickoff, so the plan does not rot against calendar
dates. W1 = the first full week after the team is assembled and the repository exists.

---

## 1. Shape of the plan

```
 W1  W2 │ W3  W4 │ W5   W6   W7   W8   W9 │ W10 W11 W12 W13 │ W14…W19 │ W20…W26
────────┼────────┼────────────────────────┼─────────────────┼─────────┼──────────
 PHASE 0│ PHASE 1│      PHASE 2           │    PHASE 3      │ PHASE 4 │ PHASE 5
 Skeleton│Identity│   KNOWLEDGE CORE       │  Distribution   │Compose  │Quality
        │        │   ★ FIRST PRODUCT ★     │                 │         │& Scale
```

**Phase 2 is the product.** At the end of W9 a user can deploy Cairn, create a knowledge base,
upload documents, and have an Agent retrieve from it over the API. Everything after W9 is
additive. The team must resist building the pipeline canvas early — it demos beautifully and
delivers nothing an Agent can call.

---

## 2. Phases

### Phase 0 — Skeleton (W1–W2)

**Goal:** a deployable empty system where the admin bootstrap works end to end.

| Deliverable | Modules | Requirements |
| --- | --- | --- |
| Repository, CI, lint, mypy, import-linter, Makefile | M00 | NFR-M-01/04 |
| Core foundation: config, errors, IDs, DB, cache, SSRF client, logging, telemetry | M00 | FR-I-04/08, NFR-O-01/04, NFR-SEC-09 |
| Alembic baseline; migrate one-shot container | M00 | NFR-D-03 |
| FastAPI skeleton with `CAIRN_ROLE` router mounting | apps/api | ADR-0002 |
| Docker Compose: postgres, redis, minio, migrate, api, nginx | deploy | NFR-D-01/02 |
| **Bootstrap admin + forced credential change, end to end** | M01, M16 | FR-A-01..08, FR-P-02 |

**Exit criteria**
- [ ] `docker compose up -d` → working system, admin password in the logs
- [ ] Journey **J1** passes including **every** interrupt variant
- [ ] `--scale api-control=3` still creates exactly one admin
- [ ] CI green: ruff, mypy strict, import-linter, tests
- [ ] Migrations run once under an advisory lock

> Bootstrap is Phase 0 rather than Phase 1 deliberately: it is the first thing every user of
> every deployment experiences, it exercises the full stack (DB → service → API → UI → guard),
> and it flushes out foundation defects while they are cheap.

---

### Phase 1 — Identity and access (W3–W4)

**Goal:** multi-user with real permissions and an audit trail.

| Deliverable | Modules | Requirements |
| --- | --- | --- |
| Durable task queue | M06 | NFR-R-03/04 |
| User CRUD, sessions, lockout, password policy | M01 | FR-A-09..13, FR-B-02 |
| Permission model, grants, effective resolution, cache | M02 | FR-B-01..07, FR-B-11/12 |
| API keys with owner intersection | M02 | FR-B-08, FR-B-13/14 |
| Break-glass admin content access | M02, M15 | FR-B-09/10 |
| Audit log + settings | M15 | FR-O-01..03, FR-O-06 |
| Admin UI: users, grants, keys, audit | M16 | FR-P-01/03 |

**Exit criteria**
- [ ] Journey **J2** passes; **J8** passes for the break-glass path
- [ ] Every endpoint carries an authorization dependency (CI route enumeration check)
- [ ] Key auth p99 < 3 ms on cache hit
- [ ] Task queue chaos test: kill workers randomly, no work lost

---

### Phase 2 — Knowledge core (W5–W9) ★

**Goal:** the shippable product. Upload → index → retrieve via API.

| Deliverable | Modules | Requirements |
| --- | --- | --- |
| Object + vector store drivers (pgvector) + conformance suite | M04, M05 | FR-G-04/07, FR-H-05 |
| KB CRUD, config, index versioning, runtime publication | M03 | FR-C-01..09, FR-G-08 |
| Document upload, dedup, lifecycle | M03 | FR-D-01..03, D-05..07, D-10 |
| Parsers, chunkers (incl. parent–child), stage pipeline | M07 | FR-F-01..07, FR-F-11 |
| Embedding service with cache and batching | M08 | FR-G-01..03 |
| **Retrieval: hybrid, fusion, rerank, explain, budget** | M09 | FR-H-01..14, FR-H-16/17 |
| **Public retrieval API** | M09 | FR-I-01..04, FR-I-06..08, FR-I-11/12 |
| UI: KB wizard, document list, chunk browser, retrieval console | M16 | FR-P-04..06 |

**Exit criteria — the release gate**
- [ ] Journeys **J3** and **J4** pass end to end
- [ ] `NFR-P-01` met: p95 < 150 ms hybrid, 1M vectors
- [ ] `NFR-P-02` met: p95 < 400 ms with rerank
- [ ] Vector store conformance suite green on pgvector and the fake
- [ ] A first-time developer completes J4 in under 15 minutes using only the docs
- [ ] `import-linter` confirms M09 imports no control-plane module
- [ ] No Postgres connection on the retrieval happy path (instrumented)

**W5 blocking spike:** `T-M07-01`, the PDF parser evaluation and licence decision. Three days.
Everything downstream of parsing depends on it and it carries a legal decision (`RISK-11`).

---

### Phase 3 — Distribution (W10–W13)

**Goal:** any Agent can use Cairn without writing code.

| Deliverable | Modules | Requirements |
| --- | --- | --- |
| **MCP server with dynamic tool descriptions** | M13 | FR-J-01..05 |
| Dify External Knowledge API | M09 | NFR-C-01 |
| Python + TypeScript SDKs; LangChain + LlamaIndex retrievers | — | FR-I-09, NFR-C-02 |
| Model gateway: providers, credentials, resilience, usage | M10 | FR-K-01..09 |
| Web crawler: scope, robots, JS, incremental, scheduling | M07 | FR-E-01..11 |
| OCR + archive support | M07 | FR-F-03, FR-D-04 |
| Quotas and usage accounting | M15 | FR-O-04/05/07 |
| UI: crawl jobs, models, chat console, zh-CN | M16 | FR-P-08, FR-N-09 |

**Exit criteria**
- [ ] Journeys **J5** and **J6** pass
- [ ] MCP verified in Claude Code and one other client by pasting a config
- [ ] Dify verified against a real Dify instance
- [ ] `NFR-P-04` met: ≥ 120 docs/hour/parse-core
- [ ] SSRF suite passes including redirect and rebinding cases
- [ ] Both locales complete

---

### Phase 4 — Composition (W14–W19)

**Goal:** power users can customize ingestion and retrieval. Read [ADR-0008](../01-architecture/05-adr/ADR-0008-pipeline-not-agent-runtime.md) first.

| Deliverable | Modules | Requirements |
| --- | --- | --- |
| Pipeline schema, validation, versioning, pure executor | M11 | FR-L-01..04, FR-L-06 |
| Node library: built-ins, LLM, control flow, MCP tool | M11 | FR-L-05, FR-J-06, FR-H-15 |
| Checkpointing + deadline-bounded retrieval execution | M11 | FR-L-08/09 |
| **Function library + gVisor sandbox** | M12 | FR-M-01..09 |
| Enrichers | M07 | FR-F-10 |
| Chunk manual editing | M03 | FR-F-09 |
| UI: pipeline editor, function editor | M16 | FR-P-07, FR-L-10, FR-M-07 |

**Exit criteria**
- [ ] Default pipelines behave identically to the hard-coded path
- [ ] **Sandbox escape suite (TC-M12-04..15) 100% green**
- [ ] **Independent security review sign-off on M12 — a hard release gate**
- [ ] A user-authored retrieval pipeline cannot breach the data-plane SLO

> If the sandbox is not ready, **ship Phase 4 without the Function Library.** Pipelines with
> built-in nodes only are still valuable. Shipping unsandboxed user code is not an option.

---

### Phase 5 — Quality and scale (W20–W26)

**Goal:** measurable quality and production-grade scale.

| Deliverable | Modules | Requirements |
| --- | --- | --- |
| Golden sets, metrics, A/B comparison, temp indexes | M14 | FR-N-01..08 |
| Qdrant driver, quantization, hybrid collection layout | M05 | FR-G-10, NFR-S-03/07 |
| Helm chart, HPA, KEDA on queue depth | deploy | NFR-D-05, NFR-S-06 |
| Grafana dashboards, alerts, runbooks | ops | NFR-O-06 |
| Backup + rehearsed restore | ops | NFR-R-08 |
| Notifications | M15 | FR-O-08 |
| Accessibility pass | M16 | FR-P-10 |
| OIDC/SSO (stretch) | M01 | FR-A-14 |

**Exit criteria**
- [ ] Journey **J7** passes
- [ ] `NFR-P-03` met: ≥ 200 RPS per replica at SLO latency
- [ ] `NFR-S-03` demonstrated: 10M vectors in one KB
- [ ] Restore drill executed and documented
- [ ] Vector store conformance green on Qdrant
- [ ] All P0 requirements satisfied

---

## 3. Critical path

```
T-M00-01 scaffold
   └─▶ T-M00-05 db ──▶ T-M06-* task queue ──▶ T-M03-* catalog ──▶ T-M07-10 stage pipeline
                                                    │                      │
                                                    ▼                      ▼
                                            T-M05-05..08 pgvector ──▶ T-M07-12 index writer
                                                    │                      │
                                                    └──────┬───────────────┘
                                                           ▼
                                              T-M09-06 concurrent search
                                                           ▼
                                              T-M09-07 fusion ──▶ T-M09-18 API   ← PHASE 2 GATE
                                                           │
                                                           ▼
                                                    T-M13-* MCP server
```

**The critical path runs through M05 → M07 → M09.** Anything that delays the vector store,
the stage pipeline, or retrieval delays the product. M02, M10, M15, and M16 have slack and
should absorb schedule pressure first.

Two items sit on the critical path and carry unusual risk:
- `T-M07-01` (parser spike, W5) — blocks all parsing work and carries a licence decision.
- `T-M05-04` (conformance suite, W6) — every driver depends on it; without it, driver
  divergence is discovered late and expensively.

---

## 4. Interface freeze schedule

Parallel work is only safe behind frozen interfaces
([module boundaries §6](../01-architecture/04-module-boundaries.md)).

| Week | Interfaces frozen | Unblocks |
| --- | --- | --- |
| W2 | `M00.errors`, `M00.db`, `M06.TaskService` | everyone |
| W3 | `M02.Principal`, `M02.dataplane` | every guarded endpoint |
| W4 | `M04.ObjectStore`, `M05.VectorStore` | M03, M07, M09 |
| W5 | `M03.CatalogService`, `KnowledgeBaseRuntime` | M07, M09, M16 |
| W5 | `M08.EmbeddingService` | M07, M09 |
| W6 | `M09.RetrievalService` | M13, M14, M16 |
| W10 | `M10.ModelGatewayService` | M07, M11, M14 |
| W13 | `M11.PipelineExecutor`, `M12.SandboxClient` | M07, M09 |

**Every frozen interface ships with a fake, maintained by the owner.** A consumer blocked on
someone else's implementation is a planning failure, not an inevitability.

---

## 5. Effort and capacity

### Effort by module (ideal engineer-days)

| Module | Days | Phase |
| --- | --- | --- |
| M00 core | 11.5 | 0 |
| M01 identity | 11.0 | 0–1 |
| M02 authz | 13.0 | 1 |
| M03 catalog | 18.5 | 2 |
| M04 objectstore | 6.0 | 2 |
| M05 vectorstore | 19.0 | 2 (11.5) / 5 (7.5) |
| M06 tasks | 12.5 | 1 |
| M07 ingestion | 42.5 | 2 (24) / 3 (14.5) / 4 (4) |
| M08 embedding | 11.5 | 2 |
| M09 retrieval | 26.0 | 2 |
| M10 modelgw | 17.0 | 3 |
| M11 pipelines | 26.5 | 4 |
| M12 functions | 19.5 | 4 |
| M13 mcp-server | 12.5 | 3 |
| M14 evaluation | 15.0 | 5 |
| M15 platform | 15.0 | 1–5 |
| M16 web UI | 51.0 | 0–5 |
| **Module subtotal** | **328.0** | |
| Deployment, CI/CD, observability, docs, security review | 60.0 | all |
| **Total** | **388.0** | |

### Capacity check

| | |
| --- | --- |
| Team | 6.5 FTE (see §7) |
| Duration | 26 weeks × 5 days |
| Gross capacity | 845 person-days |
| Ideal-to-calendar efficiency | 0.60 (reviews, meetings, defects, context switching) |
| Effective capacity | **507 ideal-days** |
| Required | **388 ideal-days** |
| **Buffer** | **119 days ≈ 31%** |

A ~30% buffer is appropriate for a project with two unknowns (parser quality, sandbox
hardening) on or near the critical path. If the buffer drops below 15%, cut from Phase 5 first —
Qdrant, notifications, accessibility, SSO — never from Phase 2.

---

## 6. Phase effort distribution

| Phase | Weeks | Ideal-days | Effective capacity | Utilization |
| --- | --- | --- | --- | --- |
| 0 | 2 | 26 | 39 | 67% |
| 1 | 2 | 42 | 39 | 108% ⚠️ |
| 2 | 5 | 108 | 98 | 110% ⚠️ |
| 3 | 4 | 78 | 78 | 100% |
| 4 | 6 | 76 | 117 | 65% |
| 5 | 7 | 58 | 136 | 43% |

Phases 1 and 2 are over-subscribed and Phases 4–5 are under-subscribed. Two deliberate
mitigations:

1. **Pull M16 frontend work forward.** The UI has 51 days spread thin; front-loading the shared
   component library in Phase 0–1 removes friction later.
2. **Pull M14 and M05-Qdrant work earlier** if Phase 2 finishes on time. Evaluation especially:
   having golden sets during Phase 3 tuning is worth more than having them in W20.

Do **not** compress Phase 2 by cutting tests. `NFR-M-03` sets 90% coverage on M09 for a reason —
it is the product, and retrieval regressions are silent.

---

## 7. Team composition

| Role | FTE | Primary modules |
| --- | --- | --- |
| Engineering lead / architect | 1.0 | M00, M06, M09, M15; reviews everything |
| Backend eng. A (security-leaning) | 1.0 | M01, M02, M12 |
| Backend eng. B | 1.0 | M03, M04, M05 |
| Backend eng. C (document processing) | 1.0 | M07, M08, M14 |
| Backend eng. D | 1.0 | M10, M11, M12 |
| Frontend eng. E | 1.0 | M16 |
| Frontend eng. F | 0.5 (from W5) | M16 |
| Product / PM | 0.5 | requirements, journeys, acceptance |
| Security reviewer | 0.2 (W1, W14–W19) | M02, M12, threat model |
| **Total** | **~6.7** | |

**Assign M09 to the strongest available engineer and do not make it shared ownership.** It is
the product; it has the tightest budgets; and the cost of a subtle ranking or degradation bug
there is far higher than anywhere else.

### Adapting to a different team size

| Team | Approach |
| --- | --- |
| **3 engineers** | Cut Phase 4 entirely (no pipelines, no functions) and Phase 5 to evaluation only. ~20 weeks to a Phase 3 product. |
| **10+ engineers** | Do not compress Phase 2 — the critical path is sequential. Parallelize instead: a second frontend, a dedicated ops engineer, Phase 5 work pulled forward, more compatibility surfaces. |
| **Agents** | See [`03-team-and-workflow.md`](03-team-and-workflow.md) §6. One module per Agent, interface-first, fakes mandatory. |

---

## 8. Milestones and decision points

| # | Week | Milestone | Decision |
| --- | --- | --- | --- |
| M1 | W2 | Skeleton deployable, J1 green | Proceed to Phase 1 |
| M2 | W5 | **Parser spike complete** | **Docling vs. MinerU vs. pypdfium2; AGPL exposure resolved** |
| M3 | W6 | Vector store conformance green | Confirm pgvector-first is viable |
| M4 | W9 | **PHASE 2 GATE — product is usable** | **Ship internally? Begin design partners?** |
| M5 | W13 | MCP + Dify + SDKs shipped | Public announcement? |
| M6 | W16 | Sandbox escape suite green | **Ship Function Library, or defer it?** |
| M7 | W19 | Pipelines complete | |
| M8 | W23 | Qdrant + 10M vector demo | Scale claim is defensible |
| M9 | W26 | All P0 requirements met | **GA** |

Milestones M2, M4, and M6 are genuine decision points where the plan may change. The rest are
progress checks.

---

## 9. What gets cut under pressure, in order

1. OIDC/SSO (`FR-A-14`) — Phase 5, P2
2. Notifications (`FR-O-08`) — Phase 5, P2
3. LLM-as-judge evaluation (`FR-N-08`) — P2
4. MCP resources (`FR-J-07`) — P2
5. Accessibility pass — defer, do not abandon; keep the AA-friendly component library
6. Elasticsearch driver — never started; pgvector + Qdrant cover the range
7. Enrichers (`FR-F-10`) — Phase 4, P2
8. **Function Library** — only if the sandbox is not ready. Ship pipelines without it.

**Never cut:** anything in Phase 2; the vector store conformance suite; the SSRF controls; the
sandbox controls if functions ship at all; audit logging; the `degraded` reporting contract.
