# Personas and User Journeys

**Document:** `00-overview/03-personas-and-journeys.md`
**Date:** 2026-08-28

---

## 1. Personas

### P1 — Platform Administrator ("Dana")

**Account type:** `admin`
**Context:** Runs the Cairn deployment for an engineering org of ~200 people. Also runs the
Kubernetes cluster and the Postgres instance. Not an ML specialist.

**Goals**
- Deploy Cairn and have it working the same afternoon
- Onboard teams without becoming a bottleneck for every KB creation
- Keep provider API keys and storage credentials safe and rotatable
- Answer "who accessed what" during a security review
- Keep costs predictable — embedding calls are billable

**Frustrations**
- Products that require reading three pages of YAML before first boot
- Admin accounts that implicitly see every tenant's confidential documents
- Provider keys pasted into config files and leaked into git

**Key requirements:** `FR-A-*`, `FR-B-*`, `FR-K-*`, `FR-O-*`, `NFR-D-*`, `NFR-SEC-*`

---

### P2 — Knowledge Engineer ("Wei")

**Account type:** `user` with `kb:manage` on their KBs
**Context:** Owns the quality of a 40,000-document internal KB. Split between English and
Chinese content, heavy on PDFs with tables.

**Goals**
- Get parsing right — tables must survive, not become word soup
- Tune chunking and retrieval and *see whether it helped*
- Compare embedding models and rerankers on real queries
- Reindex after a config change without taking retrieval down
- Explain to an Agent Developer why a specific query returned nothing useful

**Frustrations**
- Changing chunk size means "rebuild everything and hope"
- No way to see why a result ranked where it did
- Reindexing means downtime, so it never happens

**Key requirements:** `FR-C-*`, `FR-F-*`, `FR-G-*`, `FR-H-*`, `FR-L-*`, `FR-N-*`

---

### P3 — Agent Developer ("Sam")

**Account type:** consumer of an API key; may have no Cairn UI account at all
**Context:** Building an internal support Agent with Claude Code and MCP. Wants to spend zero
time on RAG.

**Goals**
- One URL, one key, one JSON payload — working retrieval in 15 minutes
- Stable contract that will not break under them
- Predictable latency they can budget around
- Citations they can render back to the user
- Ability to cap returned tokens so the context window is not blown

**Frustrations**
- APIs that return 200 with a degraded result and no indication
- Vague errors: `{"error": "failed"}`
- Having to reimplement token-budget trimming in every agent

**Key requirements:** `FR-H-*`, `FR-I-*`, `FR-J-*`, `NFR-P-*`, `NFR-C-*`

---

### P4 — Content Contributor ("Priya")

**Account type:** `user` with `kb:write`
**Context:** Technical writer. Uploads handbooks and maintains a crawled docs site.

**Goals**
- Drag 200 files in and know when they are searchable
- See exactly which files failed and why, in plain language
- Re-upload a corrected file and have only that file reprocess

**Frustrations**
- "Processing" spinners with no stage, no ETA, no error detail
- Reuploading one file triggering a full-KB rebuild

**Key requirements:** `FR-D-*`, `FR-E-*`, `FR-P-*`

---

### P5 — External Agent / Application (machine)

**Account type:** API key principal
**Context:** Automated. Retries aggressively. Rephrases queries. Runs at 3 a.m.

**Needs**
- Deterministic payload shapes; additive-only schema evolution
- `RateLimit-*` headers so it can self-throttle rather than being cut off
- Machine-readable error codes for branching
- Graceful degradation signalling, not silent quality loss

**Key requirements:** `FR-I-*`, `NFR-P-*`, `NFR-R-*`

---

## 2. Journeys

### J1 — First deployment (P1, Phase 0)

```
1. docker compose up -d
2. Reads startup logs:
      ╔════════════════════════════════════════════════════════╗
      ║  Cairn initial administrator account created            ║
      ║  username: admin                                       ║
      ║  password: 7Kq2-mVx9RtL4pZs                            ║
      ║  This password is shown once. Change it at first login.║
      ╚════════════════════════════════════════════════════════╝
3. Opens http://localhost:8080 → login with admin + generated password
4. Server responds `password_change_required`; UI shows a non-dismissable dialog
5. Dana sets a new username `dana.ops` and a new password
6. Only now is a session issued; the dashboard loads
```

**Critical behaviours exercised:** `FR-A-02`, `FR-A-03`, `FR-A-04`, `FR-A-05`, `FR-A-06`.

**Interrupt variants that MUST behave correctly:**
- Dana closes the browser at step 4 → next login shows the dialog again
- The container restarts at step 4 → next login shows the dialog again
- Dana calls any other API with a leaked token from step 4 → `403 PASSWORD_CHANGE_REQUIRED`
- Compose is scaled to 3 API replicas on first boot → exactly one admin is created

---

### J2 — Onboarding a team (P1, Phase 1)

```
1. Dana creates user `wei` with role `user`
2. Dana grants wei `kb:create` at workspace level
3. Wei logs in; forced password change (admin-set initial password)
4. Wei creates KB "Ops Handbook"
5. Wei invites priya with `kb:write` on that KB only
6. Dana reviews the audit log: 5 entries, each with actor, IP, before/after
```

**Exercises:** `FR-A-07`, `FR-B-01..06`, `FR-O-01`.

---

### J3 — Building a knowledge base (P2 + P4, Phase 2)

```
1.  Wei creates KB: name, icon, description
2.  Wei selects embedding model bge-m3 (1024-d)  ← immutable from here
3.  Wei selects storage: vector=pgvector, object=minio
4.  Wei configures chunking: markdown-aware, parent-child, 512/2048, overlap 64
5.  Wei configures retrieval defaults: hybrid, RRF, rerank on, top_k 5
6.  Priya bulk-uploads 200 PDFs
7.  UI shows a per-document table: state, stage, progress, error
8.    - 194 indexed
9.    - 4 failed: `PARSE_ENCRYPTED_PDF` — actionable message shown
10.   - 2 skipped: `DUPLICATE_CONTENT_HASH` with link to the original
11. Wei opens the chunk browser, inspects chunk boundaries on a table-heavy PDF
12. Wei runs a test query in the console with `explain` on
13. Wei sees dense found nothing, sparse found it, RRF ranked it 3rd, rerank moved it to 1st
```

**Exercises:** `FR-C-*`, `FR-D-*`, `FR-F-*`, `FR-G-*`, `FR-H-08`, `FR-P-*`.

---

### J4 — Agent integration via API (P3, Phase 2) — **the money path**

```
1. Sam is granted an API key scoped to KB "Ops Handbook", permission kb:query
2. Sam copies the key once (shown once, never again)
3. Sam issues:

   curl -X POST https://cairn.internal/v1/retrieval/query \
     -H "Authorization: Bearer cairn_sk_live_..." \
     -H "Content-Type: application/json" \
     -d '{"targets":[{"knowledge_base_id":"kb_01H..."}],
          "query":"How do I rotate the signing key?",
          "top_k":5,
          "options":{"max_context_tokens":4000}}'

4. Receives ranked hits with content, scores, and source citations in 94 ms
5. Renders citations in the agent's answer
6. Response headers tell Sam the remaining quota
```

**Exercises:** `FR-B-08`, `FR-H-*`, `FR-I-*`, `NFR-P-01`.

---

### J5 — Agent integration via MCP (P3, Phase 3)

```
1. Sam adds to their MCP client config:
     { "cairn": { "url": "https://cairn.internal/mcp",
                 "headers": { "Authorization": "Bearer cairn_sk_live_..." } } }
2. Tools appear: search_knowledge_base, list_knowledge_bases, get_document
3. The Agent calls them natively. Zero integration code written.
```

**Exercises:** `FR-J-01..05`. This is the primary distribution channel.

---

### J6 — Continuous web knowledge (P4, Phase 3)

```
1. Priya creates a crawl job: seed https://docs.example.com, depth 3,
   include ^/guide/, exclude /changelog/, schedule daily 02:00
2. First run crawls 412 pages; 8 blocked by robots.txt and reported as such
3. Nightly run: 5 pages changed → only those 5 reparse and re-embed
4. Priya sees a per-run diff: added / updated / unchanged / removed
```

**Exercises:** `FR-E-*`, `FR-G-06`, `NFR-SEC-09` (SSRF).

---

### J7 — Improving retrieval quality (P2, Phase 5)

```
1. Wei builds a golden set: 120 real queries with labelled relevant chunks
2. Baseline run: nDCG@10 = 0.61
3. Wei creates a candidate config: swap reranker bge-v2-m3 → jina-reranker-v2
4. Runs an evaluation comparison against the same golden set
5. Result: nDCG@10 = 0.68 (+11%), p95 latency +38 ms
6. Wei promotes the config; blue/green reindex not required (rerank is query-time)
7. Wei then tests chunk size 512 → 384; this DOES require reindex:
     index_version 3 builds in background while v2 serves; flip on completion
```

**Exercises:** `FR-N-*`, `FR-G-08`, `NFR-R-04`.

---

### J8 — Security review (P1, ongoing)

```
1. Auditor asks: "Can platform admins read the Legal team's contracts?"
2. Dana shows admin_content_access = break_glass
3. Dana demonstrates: attempting to read a chunk returns 403 CONTENT_ACCESS_REQUIRES_GRANT
4. Dana self-grants break-glass; access lasts 60 minutes; the KB owner is notified;
   the audit log records actor, reason, resource, and expiry
5. Auditor exports audit log as JSONL for the review period
```

**Exercises:** `FR-B-09`, `FR-B-10`, `FR-O-01..03`.

---

## 3. Journey → phase coverage matrix

| Journey | P0 | P1 | P2 | P3 | P4 | P5 |
| --- | --- | --- | --- | --- | --- | --- |
| J1 First deployment | ✅ | | | | | |
| J2 Team onboarding | | ✅ | | | | |
| J3 Building a KB | | | ✅ | | | |
| J4 API integration | | | ✅ | | | |
| J5 MCP integration | | | | ✅ | | |
| J6 Web crawling | | | | ✅ | | |
| J7 Quality improvement | | | | | | ✅ |
| J8 Security review | | ◐ | | | | ✅ |

◐ = partially available.

**Phase 2 completes J3 and J4 — at that point Cairn is a usable product.**
