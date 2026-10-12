# Cairn continuation implementation plan

**Goal:** Complete the unfinished product increments against actual source and fresh verification.
**Architecture:** Preserve the modular monolith, PostgreSQL control plane, Redis projections and Qdrant data plane. One persistent design advisor reviews deviations before implementation; the primary agent implements and verifies.
**Tech stack:** Python/FastAPI/SQLAlchemy, Qdrant, Redis, Vue/TypeScript.

## Constraints and recovered context

- Read the two requested Codex sessions and three requested Claude sessions on September 29. September 28/29 retrieval implementation is present but uncommitted; historical passing counts are not current evidence.
- Cairn first; only one complete product Compose stack at a time. bge-m3 is optional. Preserve existing changes and application data; integration fixtures only use verified disposable databases.
- Keep using `/root/design_advisor` for all design decisions and reviews. No additional agents.
- Existing approved requirements authorize continuation. Track source, test, deployment and external acceptance separately.

## Ordered deliverables

- [x] Recover history, inspect source/docs and start fresh baseline checks.
- [ ] Retrieval closure: malformed filter validation, schema-2 write invariants, bounded metadata, remaining advisor findings; regression against real services; correct stale README and contract documentation.
- [ ] MCP directory, indexed document passages and resources: revise existing design to avoid guessed overlap removal and stale parents; add store paging and permission-safe directory projections; SDK acceptance.
- [ ] S3-compatible driver, encrypted binding credentials, bounded streaming, UI configuration and MinIO contract acceptance, following reviewed September 28 design.
- [ ] Dify compatibility: settle score semantics with advisor, implement authenticated adapter and exact errors, verify HTTP contract.
- [ ] Reassess remaining Phase 3–5 work against module specs: model gateway, crawler/archive, SDKs, pipelines/functions, evaluation, quotas and production deployment. Each subsystem needs a concrete reviewed plan before implementation.
- [ ] Full backend/frontend checks, deployment/browser acceptance, record remaining external gates honestly.

## Retrieval corrections approved by the advisor

1. `tests/unit/retrieval/test_contract_options.py`: malformed `$not` and logical list entries must raise validation errors before search. `vectorstore/filters.py` must validate every filter object.
2. `tests/unit/retrieval/test_read_model_payload.py`: schema-2 index/reembedding writes with missing document facts must fail. Carry schema from frozen version configuration through catalog DTOs, enforce at write boundary; retain legacy schema-1 compatibility.
3. `core/document_metadata.py`: iterative shape validation before JSON serialization; maximum four dict/list container levels, scalar leaves do not count; reject non-JSON values. Test deeply nested arrays and valid mixed nesting.

For each fix, add the regression, run it to observe the failure, implement, rerun focused tests. Then run `uv run python -m pytest tests/unit/retrieval tests/unit/vectorstore -q`, Ruff, mypy and import-linter. Independent review precedes acceptance.

## Evidence

- Initial Ruff: passed. Initial strict mypy: 152 source files passed.
- Initial frontend Vitest: 70 passed; existing RouterLink/dialog test warnings observed.
- Backend unit baseline is running; no backend completion claim yet.
