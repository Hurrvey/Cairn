# Product Brief — Cairn

**Document:** `00-overview/01-product-brief.md`
**Status:** Approved baseline
**Date:** 2026-08-28
**Owner:** Product

---

## 1. Vision

> **Cairn is the knowledge layer that Agents call.**

Agent frameworks are proliferating — LangGraph, Dify, Claude Code, MCP clients, in-house
orchestrators. Every one of them needs the same thing underneath: a place to put documents,
turn them into retrievable knowledge, and query that knowledge quickly and reliably. Today
each team rebuilds this badly: a Postgres table, an ad-hoc chunker, a `text-embedding-3-small`
call in a loop, and no evaluation. Retrieval quality is the dominant driver of agent output
quality, and it is the part almost nobody invests in.

Cairn is a **standalone infrastructure server** for that layer. It owns knowledge production
and knowledge retrieval. It does not own agent execution. An Agent points at a URL, presents
an API key, sends a query and a knowledge base ID, and gets back ranked, cited passages.

### On the name

A cairn is a stack of stones travellers build to mark a path, so that whoever comes next can
find the way. Each passer-by adds a stone. Nobody owns it; everybody relies on it; it is
useless unless it is accurate.

That is the product. A knowledge base is built up by many contributors over time, and its
entire value is that the next traveller — here, an Agent — can trust what it points at. The
name is also a scope reminder: **a cairn marks the path; it does not walk it.**

---

## 2. Problem statement

| Pain | Consequence today |
| --- | --- |
| Ingestion pipelines are hand-rolled and fragile | Documents silently fail to index; nobody notices for weeks |
| PDF parsing is naive | Tables and multi-column layouts become scrambled text; retrieval quality collapses at the source |
| Chunking is a fixed 512-token window | Answers get cut in half; context boundaries destroy meaning |
| Retrieval is pure dense vector search | Exact terms, product codes, acronyms, and CJK queries fail |
| No evaluation | Nobody can answer "did that change make retrieval better?" |
| Retrieval is embedded in the agent app | Cannot be scaled, secured, audited, or shared independently |
| Per-team duplication | Every team maintains its own broken copy of the same pipeline |

---

## 3. Product positioning

**Cairn is:** a RAG infrastructure server — multi-tenant, permissioned, observable, horizontally
scalable, with a stable public retrieval API and an MCP interface.

**Cairn is not:** an agent framework, a chatbot builder, a conversational UI product, or a
general-purpose LLM playground.

### Comparison

| System | Category | Relationship to Cairn |
| --- | --- | --- |
| MaxKB | Agent + KB combined product | **Design reference** for KB management UX and configuration surface. Not a code source (GPLv3). |
| Dify | Agent/LLMOps platform with built-in KB | Becomes a **client** of Cairn via the External Knowledge API |
| LangChain / LlamaIndex | Libraries | Become **clients** via published retriever classes |
| Pinecone / Qdrant Cloud | Vector database | Cairn *uses* these as a storage backend; it adds ingestion, permissions, hybrid search, reranking, evaluation |
| RAGFlow | RAG engine | Closest peer. Cairn differentiates on the control/data plane split, MCP-first distribution, evaluation harness, and permission model |

### Positioning statement

> For engineering teams building Agents, who need reliable retrieval over their own documents,
> Cairn is a self-hosted knowledge base server that turns documents into a high-quality,
> low-latency retrieval API. Unlike embedding RAG inside each agent application, Cairn
> centralises ingestion, indexing, permissions, and evaluation behind one stable contract.

---

## 4. Scope boundary

This is the most important section in this document. It is the reason the project is finite.

### In scope — knowledge production

- Document upload (single, bulk, archive) — `FR-D`
- Automated web crawling with scheduled recrawl — `FR-E`
- Document parsing including layout, tables, OCR — `FR-F`
- Chunking strategies including parent–child — `FR-F`
- Metadata extraction and enrichment — `FR-F`
- Embedding generation, batched and cached — `FR-G`
- Index construction, versioning, blue/green reindex — `FR-G`

### In scope — knowledge retrieval

- Dense, sparse, and hybrid search with fusion — `FR-H`
- Cross-encoder reranking — `FR-H`
- Multi-knowledge-base federated search — `FR-H`
- Metadata filtering, parent expansion, token budgeting — `FR-H`
- Public HTTP retrieval API + MCP server + compatibility surfaces — `FR-I`, `FR-J`

### In scope — governance

- Administrator and regular user accounts, forced first-login credential change — `FR-A`
- Resource-level permissions, API keys as first-class principals — `FR-B`
- Audit logging, quotas, usage accounting — `FR-O`

### In scope — supporting capability

- **Pipelines ("Agent Workflow")**: a DAG editor over the *ingestion* and *retrieval* pipelines — `FR-L`
- **Function Library**: sandboxed user transforms plugged into pipeline slots — `FR-M`
- **Model gateway**: unified access to OpenAI, Claude, Gemini, Qwen, DeepSeek, Llama, Ollama, vLLM — `FR-K`
- **Evaluation**: golden sets, retrieval metrics, embedding/reranker A-B comparison — `FR-N`

### Explicitly out of scope (non-goals)

| Non-goal | Why | Who does it instead |
| --- | --- | --- |
| Conversational agent runtime / chat memory | Not our layer; commodity | Dify, LangGraph, Claude Code |
| Multi-turn agent loops, tool-calling orchestration | Same | The client Agent |
| End-user chat UI as a product | We ship a *testing* console only | The client application |
| Generic workflow automation (Zapier-like) | Unbounded scope | n8n, Temporal |
| Being an MCP *host* | We are an MCP *server* | The client |
| Fine-tuning / training infrastructure | Different discipline entirely | Dedicated ML platforms |
| Serving LLM inference | We proxy, we do not host generation | vLLM, Ollama, providers |

> **Interpretation rule for the team.** When a feature request arrives, ask: *does this make
> knowledge better, faster, safer, or easier to retrieve?* If yes, it is in scope. If it makes
> the **agent** smarter, it belongs to the client.

### The reframing of "Agent Workflow" and "Function Library"

These were originally requested in their MaxKB/Dify sense — chatbot orchestration. In a
knowledge infrastructure server they are retained but **repointed at the pipeline**:

| Feature | MaxKB/Dify meaning | Cairn meaning |
| --- | --- | --- |
| Agent Workflow | Chat DAG: LLM node → tool node → reply node | **Pipeline DAG**: fetch → parse → classify → extract → summarize → chunk → embed → index; and rewrite → route → search → rerank → compress |
| Function Library | Arbitrary agent tools | **Typed transforms** in fixed pipeline slots: `parse`, `chunk`, `enrich`, `filter`, `rerank` |
| MCP | Host that calls tools | **Server** that exposes knowledge as tools (primary); client for enrichment nodes (secondary) |
| Model testing | LLM playground | **Retrieval evaluation harness** — does model X raise nDCG@10 on my golden set? |

Rationale recorded in [ADR-0008](../01-architecture/05-adr/ADR-0008-pipeline-not-agent-runtime.md).

---

## 5. Target users

Detailed personas in [`03-personas-and-journeys.md`](03-personas-and-journeys.md). Summary:

| Persona | Role in Cairn | Primary need |
| --- | --- | --- |
| **Platform Administrator** | `admin` account | Deploy, configure providers and storage, manage users and quotas, audit |
| **Knowledge Engineer** | `user` account with `kb:manage` | Build KBs, tune chunking and retrieval, measure quality |
| **Agent Developer** | consumer of API keys | Call the retrieval API from an agent; needs a stable contract and good errors |
| **Content Contributor** | `user` with `kb:write` | Upload and maintain documents; needs clear ingestion status |
| **External Agent / Application** | API key principal | Machine client — low latency, high availability, predictable payloads |

---

## 6. Product principles

1. **The retrieval API is the product.** Every other surface exists to make it better. It gets
   the strongest contract guarantees, the tightest SLOs, and the most tests.
2. **Never silently degrade.** If rerank was skipped, if results were truncated to a token
   budget, if an index is mid-rebuild — say so in the response. Silent degradation destroys
   trust in infrastructure faster than an outage.
3. **Make retrieval debuggable.** `explain: true` exists because RAG is otherwise a black box.
   This is a first-class feature, not a diagnostic afterthought.
4. **Ingestion must be observable per document.** "Processing failed" with no stage and no
   reason is the single most common complaint about RAG products. Every document carries a
   state, a stage, and an error code.
5. **Quality is measured, not asserted.** No retrieval change ships without a golden-set delta.
6. **Safe by default, powerful by opt-in.** Sandboxes deny egress by default; admins cannot
   read tenant content without an audited grant; semantic caching is off by default.
7. **Operable by one person.** A single `docker compose up -d` must produce a working system.
   Every added moving part must justify its operational cost.

---

## 7. Success metrics

### Product metrics (post-launch)

| Metric | Target |
| --- | --- |
| Time from empty deployment to first successful retrieval | < 30 minutes |
| Documents ingested per hour per parse core | ≥ 120 (typical 100-page PDF) |
| Retrieval p95 latency, hybrid + rerank | < 400 ms |
| Retrieval p95 latency, hybrid without rerank | < 150 ms |
| Data-plane availability | ≥ 99.9% monthly |
| nDCG@10 on the reference golden set vs. naive dense baseline | ≥ +25% |
| Client integration effort (MCP or Dify) | Zero code |

### Engineering health metrics

| Metric | Target |
| --- | --- |
| Module boundary violations in CI | 0 |
| Requirements without an owning module | 0 |
| WBS tasks without acceptance criteria | 0 |
| Data-plane code importing control-plane modules | 0 (CI-enforced) |

---

## 8. Release strategy

Six phases, ~26 weeks. Full detail in [`../04-plan/01-development-plan.md`](../04-plan/01-development-plan.md).

| Phase | Weeks | Theme | Externally visible outcome |
| --- | --- | --- | --- |
| 0 | 1–2 | Skeleton | Deployable empty system; admin bootstrap works |
| 1 | 3–4 | Identity | Users, roles, permissions, API keys, audit |
| 2 | 5–9 | **Knowledge core** | **First shippable product**: upload → index → retrieve via API |
| 3 | 10–13 | Distribution | MCP server, crawler, model gateway, SDKs, compatibility APIs |
| 4 | 14–19 | Composition | Pipelines, Function Library, sandbox |
| 5 | 20–26 | Quality & scale | Evaluation harness, Qdrant, quantization, K8s/Helm |

**Phase 2 is the product.** Everything after is additive. The team must resist building the
pipeline canvas early — it demos beautifully and delivers nothing an Agent can call.

---

## 9. Licensing and IP

- Cairn source will be authored from scratch. Intended license: **Apache-2.0** (decision owner: Product).
- **MaxKB is GPLv3.** It may be used as a *product design and UX reference*. Reading its code
  to understand a concept is acceptable; copying, transcribing, or closely paraphrasing its
  source into this repository is **prohibited** and would impose GPLv3 on Cairn.
- All third-party dependencies MUST be license-audited in CI. Permitted: MIT, Apache-2.0, BSD,
  ISC, MPL-2.0, PSF. Requires review: LGPL. Prohibited in the distributed image: GPL, AGPL.
  - Note: **Qdrant is Apache-2.0** (fine). **MinIO server is AGPL-3.0** — it is shipped as a
    *separate unmodified container*, not linked, which is acceptable; document this and offer
    S3 as the alternative. Owner: Platform lead. See `RISK-11`.

---

## 10. Assumptions

| # | Assumption | If wrong |
| --- | --- | --- |
| A1 | Team of 5–8 engineers (or equivalent Agents) available for ~26 weeks | Rescale per [`../04-plan/01-development-plan.md`](../04-plan/01-development-plan.md) §7 |
| A2 | Self-hosted deployment is primary; SaaS is not a Phase 0–5 goal | Multi-tenancy already designed in; billing is not |
| A3 | Primary content languages are English and Chinese | Tokenizer and FTS choices assume this — see M07/M09 |
| A4 | GPU availability for local embedding/rerank is optional but expected in production | Provider APIs are the fallback path; SLOs relax by ~80 ms |
| A5 | Deployments start single-node and grow | Compose is first-class, K8s is Phase 5 |

---

## 11. Open product questions

| # | Question | Needed by | Owner |
| --- | --- | --- | --- |
| ~~PQ-1~~ | ~~Final product name~~ — **RESOLVED 2026-08-28: Cairn.** API key prefix `cairn_sk_live_`, package `cairn`, env prefix `CAIRN_` | — | — |
| PQ-2 | Open-source vs. source-available licensing | Phase 3 | Product |
| PQ-3 | Is SSO/OIDC required for the first external deployment? | Phase 1 | Product |
| PQ-4 | Default `admin_content_access` policy — `break_glass` proposed | Phase 1 | Product + Security |
| PQ-5 | Documentation language: English canonical, Chinese translation in Phase 3? | Phase 3 | Product |
| PQ-6 | **Name availability not yet verified.** Confirm PyPI `cairn`, npm `@cairn/client`, GitHub org, and domain (`cairn.io` / `.dev` / `.ai`) before Phase 1. Docs currently assume `docs.cairn.io`. | **Phase 1** | Product |
| PQ-7 | Trademark search in software classes. Known non-software holders: Cairn Energy (UK oil & gas), Cairn Homes (IE construction). Neither in software; confirm with counsel. | Phase 3 | Product |
