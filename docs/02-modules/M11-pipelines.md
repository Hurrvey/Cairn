# M11 — Pipelines (the reframed "Agent Workflow")

| | |
| --- | --- |
| **Package** | `cairn.pipelines` |
| **Layer** | L3 control plane + worker; `runtime` subpackage importable by the **data plane** |
| **Phase** | 4 |
| **Owner** | Backend eng. D |
| **Depends on** | M00, M02, M04, M05, M06, M08, M09 (facade), M10, M12 |
| **Depended on by** | M03, M07, M09 (`runtime` only), M14 |
| **Tables owned** | `pipeline`, `pipeline_version` |
| **Requirements owned** | FR-L-01..11, FR-H-15, FR-J-06, FR-F-10 |
| **ADR** | [ADR-0008](../01-architecture/05-adr/ADR-0008-pipeline-not-agent-runtime.md) |

---

## 1. Purpose and scope

Versioned DAGs applied to **ingestion** and **retrieval**. Read ADR-0008 before starting: this
is deliberately *not* a conversational agent runtime.

**In scope:** graph schema, validation, versioning, the executor, node type library,
checkpointing, testing/preview, import/export.

**Out of scope:** chat nodes, conversation memory, multi-turn loops, reply nodes, human-in-the-
loop pauses. Those belong to the client Agent.

## 1.1 The runtime/service split — critical

```
cairn/pipelines/
├── runtime.py     ← PURE executor. graph in, result out. NO persistence, NO ORM.
│                    Importable by M09 (data plane).
├── service.py     ← control plane: CRUD, versioning, publication. Imports ORM.
├── nodes/         ← node implementations
└── schema.py      ← PipelineGraph — importable by anyone
```

M09 must execute retrieval pipelines without importing the control plane (`NFR-M-02`). The
executor is therefore pure: it receives a `PipelineGraph` value (delivered via
`KnowledgeBaseRuntime`) and executes it.

---

## 2. Graph schema

```python
class PipelineGraph(BaseModel):
    kind: Literal["ingest", "retrieval"]
    nodes: list[Node]
    edges: list[Edge]
    inputs: dict[str, PortSpec]
    outputs: dict[str, PortSpec]

class Node(BaseModel):
    id: str
    type: str                     # see the node library
    config: dict[str, Any]
    position: Position            # editor layout only; ignored at execution

class Edge(BaseModel):
    source: str; source_port: str
    target: str; target_port: str
```

### Node library (`FR-L-05`)

**Ingest**

| Type | In → Out | Notes |
| --- | --- | --- |
| `source.input` | — → `RawDocument` | Entry point |
| `parse.builtin` | `RawDocument` → `ParsedDocument` | Registry parser |
| `parse.function` | `RawDocument` → `ParsedDocument` | User Function, `parse` slot |
| `chunk.builtin` | `ParsedDocument` → `Chunk[]` | Strategy from config |
| `chunk.function` | `ParsedDocument` → `Chunk[]` | `chunk` slot |
| `llm.classify` | `ParsedDocument` → `Label` | Routing / metadata |
| `llm.extract` | `ParsedDocument` → `Metadata` | Structured extraction |
| `llm.summarize` | `Chunk` → `Chunk` | Enrichment |
| `enrich.function` | `Chunk` → `Chunk` | `enrich` slot |
| `mcp.tool` | `Any` → `Any` | External MCP tool (`FR-J-06`) |
| `embed` | `Chunk[]` → `EmbeddedChunk[]` | |
| `index.output` | `EmbeddedChunk[]` → — | Terminal |

**Retrieval**

| Type | In → Out | Notes |
| --- | --- | --- |
| `query.input` | — → `Query` | Entry point |
| `rewrite.llm` | `Query` → `Query[]` | Multi-query / HyDE (`FR-H-15`) |
| `route.llm` | `Query` → `Target[]` | Pick KBs by content |
| `search.dense` / `search.sparse` / `search.hybrid` | `Query` → `Hit[]` | |
| `fuse` | `Hit[][]` → `Hit[]` | RRF / weighted |
| `filter.function` | `Hit[]` → `Hit[]` | `filter` slot |
| `rerank` | `Query, Hit[]` → `Hit[]` | |
| `rerank.function` | `Query, Hit[]` → `Hit[]` | `rerank` slot |
| `compress.llm` | `Hit[]` → `Hit[]` | Contextual compression |
| `assemble.output` | `Hit[]` → `Result` | Terminal |

**Control flow**

| Type | Notes |
| --- | --- |
| `branch.condition` | Expression on upstream output; routes to one of N |
| `map` | Applies a subgraph over a collection |
| `merge` | Joins branches |

---

## 3. Public interface

```python
# runtime.py — pure, data-plane safe
class PipelineExecutor:
    async def execute(self, graph: PipelineGraph, inputs: Mapping[str, Any],
                      ctx: ExecutionContext) -> ExecutionResult: ...

@dataclass
class ExecutionContext:
    deadline: datetime                 # HARD. Retrieval pipelines must respect the SLO.
    node_timeout: timedelta
    degraded: list[DegradationNotice]
    checkpoint: CheckpointStore | None # ingest only
    services: ServiceBundle            # injected: embedding, vectorstore, modelgw, sandbox

# service.py — control plane
class PipelineService:
    async def create(self, actor, spec) -> PipelineView: ...
    async def save_draft(self, actor, pipeline_id, graph) -> PipelineVersionView: ...
    async def validate(self, graph: PipelineGraph) -> ValidationReport: ...
    async def publish(self, actor, pipeline_id, version) -> PipelineVersionView: ...
    async def test_run(self, actor, pipeline_id, version, sample) -> TestRunResult: ...
    async def export(self, pipeline_id, version) -> dict: ...
    async def import_(self, actor, payload: dict) -> PipelineView: ...
```

---

## 4. Behaviour

### 4.1 Validation before publication (`FR-L-06`)

| Check | Failure |
| --- | --- |
| DAG is acyclic | `PIPELINE_CYCLE_DETECTED` with the cycle path |
| Exactly one entry and one terminal node | `PIPELINE_ENTRY_INVALID` |
| Every required input port is bound | `PIPELINE_PORT_UNBOUND` |
| Port types are compatible across each edge | `PIPELINE_TYPE_MISMATCH` with both types |
| No unreachable nodes | warning, not an error |
| Referenced Functions exist, are published, and match the slot | `PIPELINE_FUNCTION_INVALID` |
| Referenced models exist, are enabled, and have the right capability | `PIPELINE_MODEL_INVALID` |
| Retrieval graphs: estimated worst-case latency ≤ budget | warning with the estimate |

Type compatibility is checked statically at publish time, which is the main advantage of typed
slots over arbitrary tools — a broken pipeline is caught before it processes 40,000 documents.

### 4.2 Versioning (`FR-L-03`)

Published versions are **immutable**. Editing creates a new draft. A KB pins an exact
`(pipeline_id, version)`, so publishing a new version never silently changes a running KB's
behaviour — an explicit re-pin is required.

### 4.3 Execution — ingest

Topological order, checkpointed per node (`FR-L-08`). A worker crash resumes at the last
completed node rather than reprocessing a 500-page PDF from scratch. Node outputs above 1 MB
spill to the object store and are referenced by key.

### 4.4 Execution — retrieval, under the SLO (`FR-L-09`)

```python
async def _run_node(self, node, inputs, ctx) -> Any:
    remaining = (ctx.deadline - utcnow()).total_seconds()
    if remaining <= 0:
        ctx.degraded.append(DegradationNotice(stage=node.id, reason="deadline_exceeded"))
        return self._passthrough(node, inputs)          # skip, do not fail

    timeout = min(remaining, ctx.node_timeout.total_seconds())
    try:
        async with asyncio.timeout(timeout):
            return await self.registry[node.type].run(node.config, inputs, ctx)
    except TimeoutError:
        if node.type in REQUIRED_NODES:                 # search, assemble
            raise
        ctx.degraded.append(DegradationNotice(stage=node.id, reason="node_timeout"))
        return self._passthrough(node, inputs)          # optional nodes degrade
```

> A user-authored pipeline must not be able to break the data-plane SLO. Optional nodes are
> skipped with a `degraded` notice; only structurally required nodes can fail the request.

### 4.5 Test run (`FR-L-07`)

Executes against a sample document or query with per-node input/output capture, timing, and
token usage. Draft versions are runnable only in test mode.

### 4.6 Built-in defaults (`FR-L-04`)

`DEFAULT_INGEST_PIPELINE` and `DEFAULT_RETRIEVAL_PIPELINE` are shipped as graphs equivalent to
the hard-coded behaviour of M07 and M09. A KB with no pipeline attached uses them. **Pipelines
are an optional power feature, never a prerequisite** — the system is fully functional without a
user ever opening the editor.

---

## 5. API endpoints owned

| Method | Path | Permission |
| --- | --- | --- |
| GET/POST | `/v1/pipelines` | `pipeline:read` / `pipeline:edit` |
| GET/PATCH/DELETE | `/v1/pipelines/{id}` | |
| GET/POST | `/v1/pipelines/{id}/versions` | |
| POST | `/v1/pipelines/{id}/versions/{v}/publish` | `pipeline:edit` |
| POST | `/v1/pipelines/{id}/versions/{v}/test` (SSE) | `pipeline:run` |
| POST | `/v1/pipelines/validate` | `pipeline:edit` |
| GET | `/v1/pipelines/{id}/versions/{v}/export` | `pipeline:read` |
| POST | `/v1/pipelines/import` | `pipeline:edit` |
| GET | `/v1/pipelines/node-types` | authenticated |

---

## 6. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M11-01 | Valid ingest graph executes in topological order | FR-L-01 |
| TC-M11-02 | Valid retrieval graph produces results matching the built-in path | FR-L-02 |
| TC-M11-03 | Cycle detection reports the cycle path | FR-L-06 |
| TC-M11-04 | Type mismatch is rejected with both types named | FR-L-06 |
| TC-M11-05 | Unbound required port is rejected | FR-L-06 |
| TC-M11-06 | Published versions are immutable | FR-L-03 |
| TC-M11-07 | Publishing a new version does not change a pinned KB | FR-L-03 |
| TC-M11-08 | **Checkpoint resume: crash mid-graph resumes at the last node** | FR-L-08 |
| TC-M11-09 | **Retrieval node timeout degrades and reports; required nodes still fail** | FR-L-09 |
| TC-M11-10 | **Total retrieval pipeline execution respects the deadline** | FR-L-09 |
| TC-M11-11 | Branch node routes correctly on both paths | FR-L-05 |
| TC-M11-12 | Map node applies a subgraph across a collection | FR-L-05 |
| TC-M11-13 | Function node invokes the sandbox and returns typed output | FR-L-05 |
| TC-M11-14 | MCP tool node calls an external server | FR-J-06 |
| TC-M11-15 | Test run captures per-node input, output, and timing | FR-L-07 |
| TC-M11-16 | Export → import round-trips identically | FR-L-11 |
| TC-M11-17 | Default pipelines behave identically to the hard-coded path | FR-L-04 |
| TC-M11-18 | **`import-linter`: `runtime.py` imports no control-plane module** | NFR-M-02 |
| TC-M11-19 | Large node output spills to the object store | §4.3 |

Coverage target: **85%**.

---

## 7. Task breakdown

| Task | Description | Est (d) |
| --- | --- | --- |
| T-M11-01 | `PipelineGraph` schema + port type system | 2.0 |
| T-M11-02 | Validator (cycles, types, ports, references) | 2.0 |
| T-M11-03 | **Pure executor + `ExecutionContext`** | 2.5 |
| T-M11-04 | Node registry + built-in ingest nodes | 2.5 |
| T-M11-05 | Built-in retrieval nodes | 2.0 |
| T-M11-06 | LLM nodes (classify, extract, summarize, rewrite, compress) | 2.0 |
| T-M11-07 | Control-flow nodes (branch, map, merge) | 1.5 |
| T-M11-08 | Function node → sandbox bridge | 1.0 |
| T-M11-09 | MCP tool node | 1.0 |
| T-M11-10 | Checkpointing + resume | 1.5 |
| T-M11-11 | **Deadline enforcement + node degradation** | 1.0 |
| T-M11-12 | Versioning, publish, pin semantics | 1.5 |
| T-M11-13 | Test run with SSE trace | 1.5 |
| T-M11-14 | Import/export | 0.5 |
| T-M11-15 | Default pipeline definitions | 1.0 |
| T-M11-16 | Routers | 1.5 |
| T-M11-17 | Tests TC-M11-01..19 | 2.5 |
| | **Total** | **26.5** |
