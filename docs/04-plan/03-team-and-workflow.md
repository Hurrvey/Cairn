# Team, Workflow, and Coordination

**Document:** `04-plan/03-team-and-workflow.md`
**Status:** Normative
**Date:** 2026-08-28

How work is assigned, executed, reviewed, and integrated — by humans, by Agents, or by both.

---

## 1. Streams

Work is organized into streams, not into a flat backlog. A stream has one owner, a set of
modules, and exclusive write access to those modules' tables.

| Stream | Code | Modules | Owns tables |
| --- | --- | --- | --- |
| **Foundation** | `F` | M00, M06, M15 | `task`, `workspace_runtime`, `workspace`, `audit_log`, `usage_record` |
| **Access** | `A` | M01, M02 | `user`, `session`, `system_bootstrap`, `api_key`, `resource_grant` |
| **Knowledge** | `K` | M03, M04, M05 | `knowledge_base`, `kb_index_version`, `document`, `chunk`, `storage_binding` |
| **Ingestion** | `I` | M07, M08 | `crawl_job`, `crawl_run`, `crawl_url` |
| **Retrieval** ★ | `R` | M09, M13 | none (read-only) |
| **Extensibility** | `X` | M10, M11, M12 | `model_provider`, `model`, `secret`, `pipeline*`, `function*` |
| **Quality** | `Q` | M14, test infrastructure | `golden_set`, `golden_item`, `eval_run`, `eval_result` |
| **Web** | `W` | M16 | none |
| **Ops** | `O` | deployment, CI, observability, SDKs, docs | none |

**Exclusive table ownership is what makes parallel work safe.** Two streams never write
migrations touching the same table, so migration conflicts — the most painful kind of merge
conflict — cannot occur.

---

## 2. The coordination mechanism: interface-first

This is the single most important process rule in the project.

```
STEP 1 — Interface freeze (see the schedule in the development plan §4)
  The module owner publishes service.py + dto.py:
    · fully type-annotated
    · every method body `raise NotImplementedError`
    · docstrings stating contract and error conditions
  Reviewed and merged as ONE "interfaces" PR.

STEP 2 — Fake published (SAME PR, non-negotiable)
  tests/fakes/<module>.py — a working in-memory implementation.
  Consumers use it immediately.

STEP 3 — Parallel implementation
  Owner implements behind the frozen facade.
  Consumers build against the fake.
  NEITHER IS BLOCKED ON THE OTHER.

STEP 4 — Integration
  When the real implementation lands, consumers swap the fake for it in
  integration tests. Unit tests keep using the fake.
```

### Interface change protocol

A frozen interface may change, but only through:

1. A PR touching **only** `service.py`, `dto.py`, the fake, and the module spec.
2. Explicit approval from **every** consuming stream listed in the
   [dependency matrix](../01-architecture/04-module-boundaries.md#3-allowed-dependency-matrix).
3. The fake updated in the same PR.

> **A consumer blocked waiting for someone else's implementation is a planning failure, not an
> inevitability.** The fake removes the dependency. If a fake is missing or diverges from the
> real implementation, that is a defect against the *owner*, not the consumer.

### Fake fidelity

Fakes must match the real implementation on: error conditions, validation, ordering guarantees,
and edge-case semantics. They may differ on: performance, persistence, and approximation
(`FakeVectorStore` does exact brute-force search, not ANN).

The [conformance suites](../05-quality/01-test-strategy.md#5-contract-and-conformance-suites)
run against fakes **and** real implementations, which is what keeps them honest.

---

## 3. Branching and pull requests

```
main ──────●────────●────────●────────●──────▶  always deployable, always green
            \      /          \      /
             ●────●            ●────●            short-lived task branches
```

| Rule | Detail |
| --- | --- |
| Branch naming | `<stream>/<task-id>-<slug>` — `retrieval/T-M09-07-rrf-fusion` |
| Lifetime | ≤ 3 days. Longer means the task needed splitting. |
| One task per PR | Multi-task PRs are rejected on sight |
| Size target | < 400 changed lines excluding generated files and fixtures |
| Merge | Squash, with the task ID in the commit subject |
| CI | Must be green. No merging red, no "will fix after". |
| Reviews | 1 approval; **2 for security-gated (🔒) tasks, one from the security reviewer** |
| Migrations | One per PR. Never edit a merged migration. |

### Commit message

```
T-M09-07: implement RRF and weighted fusion

Adds reciprocal rank fusion (default, k=60) and weighted-score fusion for
callers with calibrated normalization. Per-target weights multiply each
list's contribution, implementing FR-H-04 federated weighting.

RRF is the default because dense and sparse scores are on incomparable
scales; RRF uses rank only and needs no per-corpus calibration.

Requirements: FR-H-02, FR-H-04
Tests: TC-M09-05, TC-M09-06
```

---

## 4. Definition of Done

A task is done when **all** of the following hold. This list is not negotiable per-task.

- [ ] `Done when` in the WBS entry is objectively true
- [ ] Every requirement in `Req` is satisfied and demonstrated by a test
- [ ] Tests written and passing: unit, plus integration where the task crosses a boundary
- [ ] Test docstrings cite the requirement IDs they verify
- [ ] Coverage does not decrease; module minimum met ([NFR-M-03](../00-overview/04-requirements.md))
- [ ] `mypy --strict` passes
- [ ] `ruff check` and `ruff format` clean
- [ ] `import-linter` passes
- [ ] Public functions have docstrings
- [ ] Module spec updated if behaviour or interface changed
- [ ] OpenAPI updated and SDK regenerated if endpoints changed
- [ ] No `TODO` without an owner tag
- [ ] No secret, credential, or token in code, logs, or fixtures
- [ ] Error paths return the documented `code`
- [ ] Metrics and structured logs emitted for new operations
- [ ] Reviewed and approved

**Additional for 🔒 security-gated tasks:**

- [ ] Threat model section updated
- [ ] Negative tests (attack attempts) written and passing
- [ ] Security reviewer approval recorded in the PR

---

## 5. Cadence

| Ceremony | When | Duration | Purpose |
| --- | --- | --- | --- |
| Standup | Daily | 10 min | Blockers only. Not status theatre. |
| Interface review | As scheduled | 45 min | Freeze an interface; all consumers attend |
| Phase gate | End of phase | 90 min | Walk the exit criteria; go / no-go |
| Retro | End of phase | 45 min | What to change in the next phase |
| Architecture review | On demand | — | Required before any ADR |

**Blocked-task rule:** anything blocked for more than one day is escalated at standup. The
default resolution is "use the fake and move on" — if that is not possible, the interface was
frozen too late or specified too vaguely, which is a planning defect to fix immediately.

---

## 6. Working with Agents

This documentation set is written so that a coding Agent can be handed one task and produce
integrable code. If Agents are doing the implementation, the following applies.

### 6.1 Context bundle per task

Give the Agent exactly this, and nothing more:

```
1. docs/00-overview/02-glossary.md                          (terminology is normative)
2. docs/01-architecture/06-cross-cutting-conventions.md     (how to write code here)
3. docs/02-modules/M<xx>-<name>.md                          (the module spec)
4. docs/01-architecture/03-data-model.md  §<relevant>       (if touching the schema)
5. docs/03-api/01-api-conventions.md                        (if adding endpoints)
6. The WBS row for the task
7. The service.py + dto.py of every module it depends on    (frozen interfaces)
8. tests/fakes/ for those dependencies
```

Withholding the rest is deliberate. An Agent that reads the whole corpus produces work that
drifts across boundaries — the boundaries exist precisely to limit what any one implementer
needs to know.

### 6.2 Rules for Agent contributors

| Rule | Why |
| --- | --- |
| **Implement only the assigned task.** Adjacent improvements go in a follow-up issue. | Scope creep across module boundaries is the primary failure mode |
| **Never modify another module's files.** Need a change? File a request against the owner. | Preserves exclusive ownership |
| **Never change a frozen interface.** Follow §2 instead. | Silent interface drift breaks every consumer |
| **Use the fakes.** Do not implement a dependency to unblock yourself. | Duplicate implementations are worse than waiting |
| **Cite requirement IDs** in test docstrings and the PR description. | Traceability is how we prove coverage |
| **If the spec is ambiguous, ask — do not guess.** Record the answer in the spec. | A guess becomes a permanent undocumented decision |
| **If the spec is wrong, say so before implementing.** | Specs are wrong sometimes; implementing a known-wrong spec helps nobody |

### 6.3 Parallelizable task sets

Tasks safe to run concurrently by different Agents, given frozen interfaces:

| Wave | Concurrent tasks | Precondition |
| --- | --- | --- |
| W2 | T-M00-03…13 (independent core files) | T-M00-01, T-M00-02 |
| W3 | T-M06-* ∥ T-M02-* ∥ T-M15-01..05 ∥ T-M16-03..05 | interfaces frozen W2 |
| W5 | T-M04-* ∥ T-M05-01..04 ∥ T-M07-01 ∥ T-M08-01..03 | T-M00-* |
| W6 | T-M05-05..09 ∥ T-M03-01..06 ∥ T-M07-02..09 | T-M05-04, T-M03 interface |
| W7–W8 | T-M09-01..17 ∥ T-M07-10..14 ∥ T-M03-07..15 ∥ T-M16-10..14 | W6 interfaces |
| W10+ | T-M10-* ∥ T-M13-* ∥ T-M07-15..21 ∥ T-M15-06..09 | Phase 2 gate |

Serialize where a 🔴 critical-path dependency exists; parallelize freely elsewhere.

### 6.4 Review of Agent output

Agent-produced code is reviewed by the same Definition of Done, with three additional checks
that catch the characteristic failure modes:

- **Boundary check** — did it import across a forbidden line? (`import-linter` catches this, but
  read the imports anyway.)
- **Scope check** — did it modify files outside the task's module?
- **Test honesty** — do the tests actually exercise the requirement, or do they assert that the
  implementation does what the implementation does? Tests that mock the unit under test are
  worthless; tests that assert the shape of a mock's return value are worse.

---

## 7. Tracking

| Artifact | Location | Updated |
| --- | --- | --- |
| Task board | Issue tracker; one issue per WBS task, ID in the title | Continuously |
| Requirement coverage | Generated report: requirement → tasks → tests → status | Weekly, in CI |
| Phase burndown | Ideal-days remaining vs. capacity | Weekly |
| Risk register | [`04-risk-register.md`](04-risk-register.md) | Weekly |
| ADR log | `docs/01-architecture/05-adr/` | On decision |
| Spec drift | CI check: spec files changed vs. module code changed | Per PR |

### The requirement coverage report

Generated in CI from `Req` fields in the WBS and requirement IDs in test docstrings:

```
FR-H-02  Hybrid fusion          T-M09-07  ✅ done   TC-M09-05 ✅  TC-M09-06 ✅
FR-H-08  Explain mode           T-M09-12  🔨 wip    TC-M09-15 ⬜
FR-H-16  Degradation reporting  T-M09-08  ✅ done   TC-M09-08 ✅
FR-J-04  Dynamic MCP tools      T-M13-03  ⬜ todo   TC-M13-03 ⬜  TC-M13-04 ⬜
─────────────────────────────────────────────────────────────────────────
P0 requirements:  120 total · 87 done · 19 wip · 14 todo    (72.5%)
UNCOVERED (no task):  none          ← must stay empty
UNTESTED (task, no test):  FR-O-08  ← must be empty at a phase gate
```

**"UNCOVERED" must always be empty.** A requirement with no task is a requirement nobody is
building. **"UNTESTED" must be empty at every phase gate.**

---

## 8. Escalation

| Situation | Action |
| --- | --- |
| Task blocked > 1 day | Standup escalation; default is "use the fake" |
| Interface needs changing | §2 protocol; all consumers approve |
| Spec is wrong or ambiguous | Fix the spec first, in its own PR, then implement |
| Estimate is off by > 50% | Flag immediately; re-plan the phase rather than absorbing silently |
| Architectural decision needed | Architecture review → ADR. Never decide in a PR comment. |
| Security concern | Stop, escalate to the security reviewer. Do not merge. |
| Phase gate at risk | Cut from the [documented cut list](01-development-plan.md#9-what-gets-cut-under-pressure-in-order), never from tests |
