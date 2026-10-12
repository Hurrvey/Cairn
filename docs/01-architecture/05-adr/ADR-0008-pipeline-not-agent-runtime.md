# ADR-0008 — Pipelines, not an agent runtime

**Status:** Accepted · **Date:** 2026-08-28 · **Deciders:** Product + Architecture

## Context

The original requirements listed **Agent Workflow** and **Function Library** as features,
referencing MaxKB and Dify. In those products these mean:

- **Agent Workflow** — a visual DAG of LLM calls, tool calls, conditionals, and reply nodes,
  executing a *conversation*.
- **Function Library** — arbitrary user-defined tools an agent may call.

The same requirements set Cairn's purpose as a **standalone knowledge base platform / RAG
infrastructure server**, where "Agents are only clients of this platform."

These two framings conflict directly. Building a conversational orchestration engine would:

1. Put Cairn in competition with Dify, LangGraph, and every agent framework — in *their*
   category, from behind.
2. Consume an estimated 12–16 engineer-weeks that the retrieval product needs.
3. Duplicate what clients already have. An MCP client calling Cairn *already has* an agent loop.
4. Contradict the stated scope boundary, making every future feature request ambiguous.

Meanwhile, Cairn has genuine, unmet needs for graph composition and user-supplied code — just
pointed at a different target.

## Decision

**Keep both features. Repoint them at the pipeline instead of at the conversation.**

### Agent Workflow → Pipelines

A `pipeline` is a versioned DAG applied to ingestion or retrieval, attached to a KB.

| Kind | Stages |
| --- | --- |
| `ingest` | fetch → parse → classify → extract → enrich → chunk → embed → index |
| `retrieval` | rewrite → route → search → fuse → filter → rerank → compress → assemble |

Node types: built-in stage nodes, LLM nodes (for classification, extraction, summarization,
query rewriting), conditional branches, map-over-collection, and Function nodes.

**Not included:** chat nodes, conversation memory, multi-turn loops, reply nodes, human-in-the-
loop pauses.

### Function Library → Typed transforms

A `function` implements one of five fixed slot signatures:

```python
parse:  (bytes, ParseContext)      -> ParsedDocument
chunk:  (ParsedDocument, Config)   -> list[Chunk]
enrich: (Chunk, EnrichContext)     -> Chunk
filter: (list[Hit], FilterContext) -> list[Hit]
rerank: (str, list[Hit])           -> list[Hit]
```

Not arbitrary tools — typed transforms that slot into pipeline stages. Sandboxed per `FR-M-03`.

### MCP → primarily server-side

Cairn is an **MCP server** exposing knowledge as tools (`FR-J-01..05`) — the primary
distribution channel. MCP *client* capability exists only so enrichment nodes can call external
tools (`FR-J-06`, P2).

### Model testing → retrieval evaluation

Not an LLM playground. A harness answering: *does configuration X score better than
configuration Y on my golden set?* (`FR-N-*`).

## Consequences

**Positive**
- The scope boundary becomes decidable. The team has a rule: *does this make knowledge better,
  faster, safer, or easier to retrieve?* If it makes the **agent** smarter, it is the client's job.
- Roughly 40% of the naive scope disappears without losing a listed capability.
- The pipeline engine serves a need we actually have — ingestion is genuinely a configurable DAG.
- Functions get a **type system**. Fixed signatures make them testable, composable, and
  statically checkable, which arbitrary tools never are.
- Retrieval ships in Phase 2 instead of after a workflow engine.

**Negative**
- Less impressive in a demo than a chat-flow builder. Accepted: the buyer is an engineer with a
  retrieval problem, not an audience.
- Users wanting full agent orchestration must use a client framework. Correct — and the MCP
  server makes that integration trivial.
- If the market later demands agent orchestration, this is a real pivot. Mitigated: the graph
  executor is general; adding node types is additive, and this ADR can be superseded.

## Implementation requirements

1. Pipelines are versioned and immutable once published (`FR-L-03`).
2. Retrieval pipelines run inside the data-plane latency budget with per-node timeouts; an
   exceeded node is skipped and reported in `degraded` (`FR-L-09`). A user-authored pipeline
   must not be able to break the SLO silently.
3. The executor (`cairn.pipelines.runtime`) is **pure** — graph in, result out, no persistence —
   so the data plane can import it without importing the control plane (`NFR-M-02`).
4. Built-in defaults exist for both kinds; a KB with no pipeline attached works fully. Pipelines
   are an optional power feature, never a prerequisite.

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Build the full agent runtime as requested | Competes in the wrong category; delays the actual product by ~4 months |
| Drop both features entirely | Loses genuine value — ingestion really is a configurable DAG, and custom parsers are a real need |
| Embed LangGraph | Drags in agent-runtime concepts and dependencies we deliberately exclude |
| Defer the decision | Ambiguous scope produces contradictory work; this must be settled before Phase 4 planning |
