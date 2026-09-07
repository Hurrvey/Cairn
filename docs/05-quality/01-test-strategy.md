# Test Strategy

**Document:** `05-quality/01-test-strategy.md`
**Status:** Normative
**Date:** 2026-08-28

---

## 1. Principles

1. **Every test cites a requirement.** The docstring names the requirement ID it verifies. This
   is what makes the coverage report meaningful.
2. **Never mock what you own.** Mock external providers; use real Postgres, Redis, and Qdrant
   via testcontainers. A test that mocks the database tests the mock.
3. **A flaky test is a broken test.** Fix it or delete it. `@pytest.mark.flaky` is banned —
   quarantined flaky tests train the team to ignore red CI, which is worse than having no test.
4. **Test the contract, not the implementation.** Tests asserting internal call sequences break
   on every refactor and catch nothing.
5. **Quality is measured.** Retrieval changes are validated against golden sets, not opinions.

---

## 2. The pyramid

```
        ╱ E2E ╲             ~30 tests    Playwright, full journeys, nightly + pre-release
      ╱─────────╲
    ╱ Integration ╲         ~250 tests   Real deps via testcontainers, per PR
  ╱─────────────────╲
╱       Unit          ╲     ~1200 tests  Fakes, in-memory, per commit, < 60 s total
────────────────────────
     Contract / Conformance  ~120 tests  Cross-implementation, per PR
```

| Layer | Scope | Deps | Runtime budget |
| --- | --- | --- | --- |
| Unit | One function or class | Fakes only | < 60 s whole suite |
| Contract | One interface, all implementations | Real + fake | < 5 min |
| Integration | One module + real infrastructure | testcontainers | < 10 min |
| E2E | Full journeys through the UI | Full Compose stack | < 20 min |
| Load | SLO verification | Full stack, seeded corpus | < 15 min |
| Security | Attack attempts | Real sandbox, real network policy | < 5 min |

---

## 3. Layout and naming

```
tests/
├── unit/<module>/test_<unit>.py
├── contract/{vectorstore,objectstore,modelgw}/
├── integration/<module>/
├── e2e/test_j<n>_<journey>.py
├── load/
├── security/
├── fakes/<module>.py            ← maintained by the MODULE OWNER
├── factories/                   ← test data factories
└── fixtures/corpus/             ← the 30-document reference corpus
```

```python
def test_login__user_must_change_password__returns_change_token_without_session():
    """FR-A-04: a flagged user receives a scope-limited change token and NO session."""
```

`test_<unit>__<condition>__<expected>`. The failure name alone should identify the defect.

---

## 4. Fixtures and factories

Factories, never hand-built dicts — a hand-built dict silently rots when the model changes.

```python
# tests/factories/knowledge.py
class KnowledgeBaseFactory(Factory):
    class Meta: model = KnowledgeBase
    name = Sequence(lambda n: f"KB {n}")
    embedding_dim = 1024
    metric = "cosine"
    chunk_config = LazyFunction(ChunkConfig.default)
    active_index_version = 1
```

### The reference corpus

`tests/fixtures/corpus/` — 30 documents committed to the repository, exercising every parsing
challenge that matters:

| Category | Count | Exercises |
| --- | --- | --- |
| Academic PDFs, two-column | 5 | Reading order (`TC-M07-02`) |
| Financial reports with tables | 5 | Table structure (`TC-M07-03`) |
| Scanned PDFs | 3 | OCR routing (`TC-M07-04`) |
| Chinese documents | 4 | CJK tokenization, chunking (`TC-M07-06`) |
| Mixed EN/CN | 2 | Language detection |
| DOCX / PPTX / XLSX | 6 | Office parsers |
| HTML with heavy boilerplate | 3 | Main-content extraction |
| Malformed / encrypted / empty | 2 | Error paths |

Each has a committed expected-output snapshot. **Parser regressions fail CI**, which is the only
practical defence against RISK-01.

### Golden sets

`tests/fixtures/golden/` — labelled `(query, relevant_chunk_ids)` over the reference corpus.
Used to assert that hybrid beats dense (`TC-M09-03`) and that rerank improves nDCG
(`TC-M09-07`), and as the regression baseline for every retrieval change.

---

## 5. Contract and conformance suites

The most valuable tests in the project. One suite per pluggable interface, run against **every**
implementation including the fake.

```python
# tests/contract/vectorstore/test_conformance.py
@pytest.fixture(params=["fake", "pgvector", "qdrant"])
def store(request) -> VectorStore: ...

async def test_filter_in_on_array_field__matches_intersection(store):
    """TC-M05-05 / FR-H-05: `$in` on an array matches if the intersection is non-empty.
    Divergence here means users get different results per backend — a correctness bug
    that no test the user writes will catch."""
```

A driver is not "done" until it passes. This is the mitigation for RISK-07, and it is also what
keeps fakes honest: a fake that diverges from the real implementation fails the same suite.

---

## 6. Coverage requirements (`NFR-M-03`)

| Scope | Line | Branch |
| --- | --- | --- |
| Overall | 80% | 70% |
| M00 core | 90% | 85% |
| M01 identity | 90% | 85% |
| M02 authz | **90%** | **85%** |
| M06 tasks | **90%** | **85%** |
| M09 retrieval | **90%** | **85%** |
| M12 functions | 85% + **100% of the escape suite** | 80% |
| M07 parsers | 80% (corpus snapshots carry the weight) | 70% |
| M16 web | 70% + E2E journeys | — |

CI fails on any decrease. Coverage is a floor, not a goal — 90% coverage with tests that assert
nothing is worse than 70% with tests that assert the contract.

---

## 7. Security testing

Runs on every PR touching a 🔒 task. **Release-gating.**

| Suite | Tests | Gate |
| --- | --- | --- |
| SSRF | `TC-M00-05..08`, `TC-M07-19/20` — metadata endpoints, redirect hops, DNS rebinding | Phase 2 |
| Sandbox escape | `TC-M12-04..15` — `os.system`, file reads, network, fork bomb, memory bomb, `ctypes`, cross-run state | **Phase 4, hard gate** |
| Archive safety | `TC-M07-24/25` — zip-slip, decompression bomb | Phase 3 |
| Authorization | `TC-M02-*` — intersection, expiry, break-glass, isolation | Phase 1 |
| Secret handling | `TC-M10-01..05` — encryption, no leak in logs/API/traces | Phase 3 |
| Auth hardening | `TC-M01-*` — enumeration timing, lockout, forced-change bypass | Phase 0–1 |

Plus continuous: dependency vulnerability scan, licence scan, secret scan on every commit.

---

## 8. Performance testing

Load tests run in CI from **Phase 2**, not discovered in Phase 5.

| Test | Asserts | Requirement |
| --- | --- | --- |
| `test_retrieval_slo` | p95 < 150 ms at 200 RPS, 1M vectors | NFR-P-01, NFR-P-03 |
| `test_retrieval_rerank_slo` | p95 < 400 ms with rerank | NFR-P-02 |
| `test_ingestion_throughput` | ≥ 120 docs/hour/core | NFR-P-04 |
| `test_embedding_throughput` | ≥ 2000 chunks/s (GPU) | NFR-P-05 |
| `test_task_claim_throughput` | ≥ 1000 claims/s at 100k ready | — |
| `test_authz_overhead` | p99 < 3 ms cache hit | NFR-P-06 |
| `test_vector_search_latency` | p95 < 25 ms, 1M vectors | M05 §9 |

Results are tracked over time; a **10% regression fails the build**. Latency regressions
accumulate invisibly one PR at a time otherwise.

---

## 9. Chaos and resilience

| Scenario | Expected | Requirement |
| --- | --- | --- |
| Kill workers randomly during a 1000-document ingest | Every document completes | NFR-R-04/05 |
| Stop Postgres during retrieval | Serves from cache, then degrades cleanly | NFR-R-02 |
| Stop Redis | Falls back to Postgres, latency rises, no errors | — |
| Stop the vector store | Affected KBs fail with clear errors; others unaffected | FR-H-17 |
| Provider returns 500s | Circuit opens, fails fast, recovers | FR-K-07 |
| Fill the disk during ingest | Fails cleanly, no corruption | — |
| Kill the API mid-upload | No orphaned objects, no half-registered documents | NFR-R-03 |
| Restart between login and credential change | Change still required | FR-A-06 |

Run nightly against the full Compose stack.

---

## 10. CI pipeline

```
on: pull_request
  ├─ lint            ruff check + format --check           ~20 s
  ├─ typecheck       mypy --strict                          ~60 s
  ├─ boundaries      import-linter                          ~10 s   ← blocks boundary erosion
  ├─ routes          every endpoint has an authz dependency ~10 s
  ├─ unit            pytest tests/unit                      ~60 s
  ├─ contract        pytest tests/contract  (testcontainers) ~5 min
  ├─ integration     pytest tests/integration               ~10 min
  ├─ security        pytest tests/security (if 🔒 touched)  ~5 min
  ├─ coverage        fail on decrease                       ~5 s
  ├─ deps            pip-audit + licence scan               ~30 s
  ├─ secrets         gitleaks                               ~10 s
  ├─ openapi         spec regenerated and committed         ~20 s
  ├─ sdk             clients regenerated                    ~40 s
  └─ spec-drift      module code changed ⇒ spec reviewed    ~5 s

on: push to main
  └─ + load tests, E2E

nightly
  └─ + chaos, compatibility matrix against real clients, full corpus regression
```

**Nothing merges red.** No "fix it after". A red main branch is a full-team stop.

---

## 11. Test data policy

- No production data, ever. No real PII in fixtures.
- The reference corpus is public-domain or synthetic, with provenance recorded.
- Provider calls in tests use recorded cassettes; live calls only in a manually-triggered job.
- Test API keys are clearly fake (`cairn_sk_test_...`) and rejected by production config.
- Seed data is deterministic — same seed, same corpus, same golden set, every run.
