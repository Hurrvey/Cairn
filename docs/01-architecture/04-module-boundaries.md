# Module Boundaries

**Document:** `01-architecture/04-module-boundaries.md`
**Status:** Normative — enforced in CI
**Date:** 2026-08-28

This document defines what each module owns, who may import whom, and how the rules are
enforced. **These rules are the reason parallel development is safe.** Violating them is a
CI failure, not a style opinion.

---

## 1. Module registry

| ID | Package | Layer | Owns (tables) | Phase | Spec |
| --- | --- | --- | --- | --- | --- |
| M00 | `cairn.core` | foundation | — | 0 | [M00](../02-modules/M00-core.md) |
| M01 | `cairn.identity` | control | `user`, `session`, `system_bootstrap` | 0–1 | [M01](../02-modules/M01-identity.md) |
| M02 | `cairn.authz` | both | `api_key`, `resource_grant` | 1 | [M02](../02-modules/M02-authz.md) |
| M03 | `cairn.catalog` | control | `knowledge_base`, `kb_index_version`, `document`, `chunk`, `storage_binding` | 2 | [M03](../02-modules/M03-catalog.md) |
| M04 | `cairn.objectstore` | driver | — | 2 | [M04](../02-modules/M04-objectstore.md) |
| M05 | `cairn.vectorstore` | driver | — (owns the vector read model) | 2 | [M05](../02-modules/M05-vectorstore.md) |
| M06 | `cairn.tasks` | shared | `task`, `workspace_runtime` | 1 | [M06](../02-modules/M06-tasks.md) |
| M07 | `cairn.ingestion` | worker | `crawl_job`, `crawl_run`, `crawl_url` | 2–3 | [M07](../02-modules/M07-ingestion.md) |
| M08 | `cairn.embedding` | shared | — | 2 | [M08](../02-modules/M08-embedding.md) |
| M09 | `cairn.retrieval` ★ | **data** | — | 2 | [M09](../02-modules/M09-retrieval.md) |
| M10 | `cairn.modelgw` | shared | `model_provider`, `model`, `secret` | 3 | [M10](../02-modules/M10-modelgw.md) |
| M11 | `cairn.pipelines` | control+worker | `pipeline`, `pipeline_version` | 4 | [M11](../02-modules/M11-pipelines.md) |
| M12 | `cairn.functions` | control+worker | `function`, `function_version` | 4 | [M12](../02-modules/M12-functions.md) |
| M13 | `cairn.mcpserver` ★ | **data** | — | 3 | [M13](../02-modules/M13-mcp-server.md) |
| M14 | `cairn.evaluation` | control | `golden_set`, `golden_item`, `eval_run`, `eval_result` | 5 | [M14](../02-modules/M14-evaluation.md) |
| M15 | `cairn.platform` | control | `workspace`, `audit_log`, `usage_record` | 1 | [M15](../02-modules/M15-platform.md) |
| M16 | `apps/web` | frontend | — | 1–5 | [M16](../02-modules/M16-web-ui.md) |

★ = data plane, SLO-bound.

**Table ownership is exclusive.** Only the owning module may write to its tables. Other
modules read through the owner's service facade. This is what lets two developers work on M03
and M07 simultaneously without merge conflicts in the data layer.

---

## 2. Layer rules

```
   ┌──────────────────────────────────────────────────────────┐
   │ L4  ENTRYPOINTS   apps/api, apps/worker                  │  may import anything
   ├──────────────────────────────────────────────────────────┤
   │ L3  CONTROL       M01 M03 M10 M11 M12 M14 M15            │  ↓ L2, L1, L0
   │ L3d DATA      ★   M09 M13                                │  ↓ L2, L1, L0 only
   ├──────────────────────────────────────────────────────────┤
   │ L2  DOMAIN SVC    M02 authz, M06 tasks, M08 embedding    │  ↓ L1, L0
   ├──────────────────────────────────────────────────────────┤
   │ L1  DRIVERS       M04 objectstore, M05 vectorstore       │  ↓ L0
   ├──────────────────────────────────────────────────────────┤
   │ L0  FOUNDATION    M00 core                               │  imports nothing internal
   └──────────────────────────────────────────────────────────┘
```

### Rule 1 — Downward only
A module may import only from strictly lower layers. No upward imports, ever.

### Rule 2 — No sideways imports within a layer, except via facade
Within L3, `M03` may call `M11` only through `cairn.pipelines.service`. Never
`cairn.pipelines.models` or `cairn.pipelines.repository`.

---

## 2.1 Known gaps in this layer model — RESOLVE BEFORE THE NAMED MODULE LANDS

**Status:** open · **Raised:** 2026-08-31 by `import-linter` during T-OPS-01 · **Owner:** Engineering lead

Encoding this diagram as an `import-linter` layers contract during Phase 0 proved
that three documented relationships **cannot hold simultaneously** with the layer
assignments above. This is a defect in this document, not in the tooling.

| # | Conflict | Documented in |
| --- | --- | --- |
| G1 | `M08 embedding` (L2) → `M10 modelgw` (L3) is an **upward** import | §3 matrix, M08 spec §1 |
| G2 | `M02 authz` → `M15 platform` is **sideways** — both are placed in their own layers, but the matrix permits the import | §3 matrix, M02 §4.5 |
| G3 | `M11 pipelines` ⇄ `M09 retrieval` is **mutual**; a whole-module layer stack cannot express it at all | §3 matrix |
| G4 | **Routers are a presentation layer this model does not have.** A router needs its own module's service *and* the authz guards, so any module below `authz` produces an upward import the moment it exposes an endpoint. Surfaced by `cairn.platform.router → cairn.authz.deps` in Phase 1. | §5 module layout |
| G5 | The illustrative stack in §6 places `M03 catalog`, `M15 platform`, and `M10 modelgw` on **one layer**, but `catalog` imports both (`platform.audit` for audit records, `modelgw.catalog` to validate the embedding model at KB creation). Siblings may not import each other, so that stack is unsatisfiable. Surfaced by `T-M03-01` in Phase 2. | §6 snippet |

G5 was resolved by *stratifying* rather than relaxing: `catalog` sits above
`platform`, and `modelgw` drops to the layer just above `core`. That is not a
concession — it is the tightest true statement about each module. `modelgw` really
does import nothing but `core`, and pinning it low means every later module may
depend on it while it can never acquire a dependency on them. §6 below now shows
the stack as implemented rather than as first sketched.

G4 has a clean structural fix and should be taken before Phase 2 adds four more
routers: **move `router.py` out of each module into `apps/api/routers/`.** That is
what Rule 6 ("entrypoints do the wiring") already implies, and it makes the
dependency direction honest — a router is an interface adapter, not domain code.
Until then, `.importlinter` carries two documented `ignore_imports` entries.
(The catalog router needed none: `catalog` already sits above `authz`, so its
guard imports point downward.)

G3 is the other interesting one. It is not really circular — `M09` imports
`cairn.pipelines.runtime` (the pure executor) while `M11` imports
`cairn.retrieval.service`. The cycle exists only at module granularity. The fix
is to treat `pipelines.runtime` as a **separate, lower layer** from
`pipelines.service`, which is what [ADR-0008](05-adr/ADR-0008-pipeline-not-agent-runtime.md)
already requires for a different reason.

**Interim policy.** `.importlinter` names only modules that exist. Adding a module
to the layer stack is part of that module's own task, because that is when its
real dependency shape is known. The two `forbidden` contracts — data-plane
isolation and ORM privacy — carry the weight in the meantime; they are the ones
that guard the failure mode we actually care about.

**Proposed resolution** (to be confirmed when M08/M10 are specified in Phase 3):

```
L3   identity · catalog · pipelines.service · functions · evaluation · retrieval · mcpserver
L2c  modelgw
L2b  authz · embedding · pipelines.runtime
L2a  platform
L1b  tasks
L1a  objectstore · vectorstore
L0   core
```

Verify against the §3 matrix before adopting; if it holds, update both this
section and the diagram above in the same PR.

### Rule 3 — **Data plane isolation** (`NFR-M-02`)
`M09` and `M13` MUST NOT import from `M01, M03, M10, M11, M12, M14, M15`.

They may import: `M00`, `M02` (the cache-only subset), `M05`, `M08`, `M06` (read-only).

> **Why this is worth the friction.** Retrieval must scale, deploy, and fail independently.
> The moment `M09` imports `cairn.catalog.models.KnowledgeBase` "for just one field", the data
> plane inherits the control plane's ORM, its migrations, and its startup cost — and the
> ability to extract or scale it separately is quietly gone. The KB config the data plane
> needs arrives as a cached DTO, never as an ORM object.

### Rule 4 — No cross-module ORM imports (`NFR-M-06`)
SQLAlchemy models are private to their module. Crossing a boundary means a DTO.

```python
# ❌ FORBIDDEN
from cairn.catalog.models import KnowledgeBase

# ✅ REQUIRED
from cairn.catalog.dto import KnowledgeBaseView
from cairn.catalog.service import CatalogService
```

### Rule 5 — Drivers know nothing about the domain
`M04` and `M05` accept primitives and driver DTOs. `M05.search()` takes a `Namespace` and a
`VectorQuery`, never a `KnowledgeBase`.

### Rule 6 — Entrypoints do the wiring
Dependency injection, router mounting, and lifecycle live in `apps/`. Modules never import
`apps`.

---

## 3. Allowed dependency matrix

Row may import column. `F` = via facade only.

| ↓ imports → | M00 | M01 | M02 | M03 | M04 | M05 | M06 | M07 | M08 | M09 | M10 | M11 | M12 | M13 | M14 | M15 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **M00** core | — | | | | | | | | | | | | | | | |
| **M01** identity | ✅ | — | | | | | | | | | | | | | | F |
| **M02** authz | ✅ | F | — | | | | | | | | | | | | | F |
| **M03** catalog | ✅ | F | F | — | ✅ | ✅ | ✅ | | ✅ | | F | F | | | | F |
| **M04** objectstore | ✅ | | | | — | | | | | | | | | | | |
| **M05** vectorstore | ✅ | | | | | — | | | | | | | | | | |
| **M06** tasks | ✅ | | | | | | — | | | | | | | | | |
| **M07** ingestion | ✅ | | F | F | ✅ | ✅ | ✅ | — | ✅ | | F | F | F | | | F |
| **M08** embedding | ✅ | | | | | | | | — | | F | | | | | |
| **M09** retrieval ★ | ✅ | ❌ | F* | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ | — | ❌ | F* | ❌ | ❌ | ❌ | ❌ |
| **M10** modelgw | ✅ | | | | | | | | | | — | | | | | F |
| **M11** pipelines | ✅ | | F | F | ✅ | ✅ | ✅ | | ✅ | F | F | — | F | | | F |
| **M12** functions | ✅ | | F | | ✅ | | ✅ | | | | | | — | | | F |
| **M13** mcpserver ★ | ✅ | ❌ | F* | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | — | ❌ | ❌ |
| **M14** evaluation | ✅ | | F | F | | ✅ | ✅ | | ✅ | F | F | F | | | — | F |
| **M15** platform | ✅ | | | | | | ✅ | | | | | | | | | — |

`F*` = restricted facade. `M09`/`M13` may use only `cairn.authz.dataplane` (cache-only
resolution, no ORM) and `cairn.pipelines.runtime` (the pure graph executor, no persistence).

---

## 4. Enforcement

`.importlinter` at the repository root, run in CI and pre-commit (`NFR-M-01`):

```ini
[importlinter]
root_package = cairn

[importlinter:contract:layers]
name = Cairn layered architecture
type = layers
; As implemented. Modules not yet written are shown commented, to be slotted in
; by their own task once their real dependency shape is known (see §2.1).
layers =
    cairn.catalog | cairn.identity
    cairn.authz
    cairn.platform
    cairn.tasks
    cairn.modelgw | cairn.objectstore | cairn.vectorstore
    cairn.core

[importlinter:contract:dataplane]
name = Data plane must not import control plane
type = forbidden
source_modules =
    cairn.retrieval
    cairn.mcpserver
forbidden_modules =
    cairn.identity
    cairn.catalog
    cairn.modelgw
    cairn.functions
    cairn.evaluation
    cairn.platform

[importlinter:contract:no_cross_orm]
name = ORM models are module-private
type = forbidden
source_modules = cairn
forbidden_modules =
    cairn.identity.models
    cairn.catalog.models
    cairn.authz.models
    cairn.modelgw.models
    cairn.pipelines.models
    cairn.functions.models
    cairn.evaluation.models
    cairn.platform.models
ignore_imports =
    cairn.identity.* -> cairn.identity.models
    cairn.catalog.* -> cairn.catalog.models
    cairn.authz.* -> cairn.authz.models
    cairn.modelgw.* -> cairn.modelgw.models
    cairn.pipelines.* -> cairn.pipelines.models
    cairn.functions.* -> cairn.functions.models
    cairn.evaluation.* -> cairn.evaluation.models
    cairn.platform.* -> cairn.platform.models

[importlinter:contract:apps_isolation]
name = Modules must not import entrypoints
type = forbidden
source_modules = cairn
forbidden_modules = apps
```

---

## 5. Standard module layout

Every module follows this shape. Deviations require a note in the module spec.

```
cairn/<module>/
├── __init__.py          # re-exports the public surface only
├── service.py           # ← THE PUBLIC FACADE. other modules import only this + dto
├── dto.py               # ← Pydantic DTOs crossing the boundary
├── models.py            # SQLAlchemy ORM — PRIVATE
├── repository.py        # data access — PRIVATE
├── errors.py            # module-specific errors, subclassing cairn.core.errors
├── router.py            # FastAPI routes (if the module owns endpoints)
├── schemas.py           # request/response models for router.py
├── tasks.py             # task handlers registered with M06 (if any)
├── config.py            # module settings, composed into the root Settings
└── <domain>.py …        # internal implementation
```

**`service.py` is the contract.** When a task says "implement M03", it means: write
`service.py` and `dto.py` first, get them reviewed, then implement behind them. Other teams
code against the facade signature from day one — this is what unblocks parallel work.

---

## 6. Interface-first parallel development protocol

The mechanism that lets 6+ developers or Agents work simultaneously:

```
Week N       — Interface freeze
               All module owners publish service.py + dto.py with full type
               annotations and `raise NotImplementedError` bodies.
               Reviewed and merged in one "interfaces" PR per module.

Week N+1..   — Parallel implementation
               Each owner implements behind their frozen facade.
               Consumers code against the facade using generated fakes.
               Fakes live in `tests/fakes/<module>.py`, maintained by the OWNER.

Any time     — Interface change
               Requires: (a) a PR touching only service.py/dto.py and the spec,
               (b) sign-off from every consuming module owner listed in §3,
               (c) the fake updated in the same PR.
```

**Every module owner MUST maintain a fake.** A consumer blocked on someone else's
implementation is a planning failure — the fake removes the dependency.

```python
# tests/fakes/vectorstore.py — owned by the M05 owner
class FakeVectorStore(VectorStore):
    """In-memory VectorStore. Brute-force search; exact, not approximate.
    Semantics must match the real drivers for: dim validation, filter operators,
    namespace isolation, and delete-by-filter."""
```

---

## 7. Shared code policy

| Situation | Rule |
| --- | --- |
| Two modules need the same helper | It goes in `M00 core` — only if genuinely generic |
| Two modules need the same domain concept | One owns it; the other uses the facade. **Do not duplicate.** |
| A module needs another's ORM model | It does not. It needs a DTO. Ask the owner to add a field. |
| Something feels like it belongs in `core` but is domain-specific | It does not go in `core`. `core` has zero domain knowledge. |

`M00 core` is deliberately small: config, errors, IDs, time, pagination, transaction helper,
logging, tracing, HTTP client factory. If `core` starts importing anything domain-shaped, the
architecture has failed.

---

## 8. Frontend boundaries (M16)

```
apps/web/src/
├── api/            # GENERATED from OpenAPI. never hand-edited.
├── shared/         # design tokens, layout, generic components
├── features/
│   ├── auth/       ├── users/     ├── grants/    ├── api-keys/
│   ├── knowledge/  ├── documents/ ├── chunks/    ├── retrieval-console/
│   ├── models/     ├── pipelines/ ├── functions/ ├── evaluation/
│   └── admin/
└── app/            # router, guards, i18n, providers
```

Rules: features never import from each other (shared code moves to `shared/`); `api/` is
regenerated in CI and a diff fails the build if it was hand-edited; each feature maps to one
or more backend modules and is assignable to one developer.

---

## 9. Ownership assignment

| Stream | Modules | Suggested owner |
| --- | --- | --- |
| **Foundation** | M00, M06, M15 | Backend lead |
| **Access** | M01, M02 | Backend eng. A (security-minded) |
| **Knowledge** | M03, M04, M05 | Backend eng. B |
| **Ingestion** | M07, M08 | Backend eng. C (document-processing depth) |
| **Retrieval** ★ | M09, M13 | Backend lead or strongest backend eng. |
| **Extensibility** | M10, M11, M12 | Backend eng. D |
| **Quality** | M14 + test infra | Backend eng. C (Phase 5) |
| **Frontend** | M16 | Frontend eng. E (+ F from Phase 2) |

**M09 is the product.** Assign it to the strongest available engineer and do not let it become
a shared-ownership module.
