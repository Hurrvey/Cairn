# Agent MCP test integration proposal

Date: 2026-09-18. Status: IMPLEMENTED; real-Cairn CLI/WS transport acceptance passed with a
deterministic chat model. External live-LLM and full business deployment acceptance remain OPEN.

## Existing integration seams verified

Target repository: `D:/code/ai-smart-screen-digital-human`, clean at inspection.
`internal/agent/deps.go` defines Retriever; `internal/agent/loop.go` already retrieves before LLM
generation; `internal/agent/prompt.go` inserts knowledge into the system prompt. Both `router/init.go`
and `test/agent-chat/main.go` currently instantiate the local HybridRetriever. The latter already
supports `-rag`, `-config`, `-once`, `-pretty` and environment-based chat-model configuration.

## Proposed smallest testable change

Implement a dedicated MCP retriever adapter using the official Go SDK StreamableHTTP transport.
Connect/discover at initialization, call only `search_knowledge_base`, check transport and isError
separately, decode structuredContent results into existing KnowledgeChunk with original citation IDs.
Reuse client with bounded concurrency/cancellation; close on application shutdown; recover connection
after a Cairn MCP restart without duplicate cross-session context or unbounded retries.

Add backend selection `local|cairn_mcp` preserving old behavior by default. Select backend in both
production composition and local test entrypoint. New proposed settings: RAG_BACKEND, CAIRN_MCP_URL,
CAIRN_API_KEY, CAIRN_KB_ID, CAIRN_MCP_TIMEOUT; keep RAG_TOP_K bounded to1..20 for Cairn. These variables
are now implemented. Do not reuse old BM25/vector weights or0.05threshold for RRF scores.
Do not initialize Qdrant, local JSONL/dictionary or the old embedding service in Cairn mode.

Keep guaranteed pre-retrieval for first end-to-end testing. Model-decided MCP tool invocation is a
separate optional phase; don't run both modes unconditionally and retrieve twice. Retain current
speech/session/WS/business tools. Strengthen reference-data instruction boundary and distinguish
empty evidence from unavailable retrieval; test mode must fail explicitly rather than appear green
by silently continuing without RAG. Log request IDs/latency/hit counts, not keys/content.

## SDK/toolchain decision

Agent go.mod declaresGo1.22.0; installed local toolchain isGo1.25.4. Official Go SDKv1.6.1 go.mod
requiresGo1.25.0 and supports protocol2025-11-25. Pin a reviewed compatible version and align CI/build
toolchain before implementation; do not blindly add latest or change compiler requirements silently.
References: https://github.com/modelcontextprotocol/go-sdk and
https://raw.githubusercontent.com/modelcontextprotocol/go-sdk/v1.6.1/go.mod .

## Acceptance sequence

1. Verify Agent host can reach configured Cairn URL; current local-only deployment requires explicit
   LAN binding/firewall/Host allowlist changes before using192.168.200.137 from another machine.
2. Standalone MCP client handshake/discovery/search without invoking a chat model.
3. Adapter tests: schema/output mapping, isError, timeout/cancel,401/403/429/503, no-key logging,
   credential isolation, reconnect, disabled/local backend compatibility and prompt safety.
4. Existing `go run ./test/agent-chat -rag -once ... -pretty` against configured real MCP and LLM.
   Use a unique disposable KB fact so general model knowledge cannot masquerade as retrieval.
5. Repeat via production WS path. Stop MCP in Cairn UI and verify explicit failure/degradation;
   restart and verify recovery. Agent repository changes were explicitly approved on September18.

## September18 execution contract

User explicitly approved implementation. Agent branchrag starts clean at662972d, Cairn dirty work
preserved. No AGENTS.md applies. Local Go1.25.4 supports the selected official MCP SDKv1.6.1;
go.mod toolchain floor will be raised explicitly to1.25.0 and build docs updated. No commits/branches.

- Sole sol/high worker owns `internal/rag/cairn_mcp.go` and adapter tests only. Interface:
  `CairnMCPConfig{URL,APIKey,KnowledgeBaseID string; Timeout time.Duration; TopK int; MaxConcurrency int}`,
  `NewCairnMCPRetriever(ctx, config, *http.Client) (*CairnMCPRetriever,error)`, Retriever.Retrieve,
  `Close() error`. Official SDKv1.6.1; strict bounded input/output, bearer headers, no raw error leaks,
  per-instance credentials, bounded cancellation-aware concurrency, connection reuse/reconnect.
- Parent owns config/backend factory, Agent error policy/prompt, production and CLI entrypoints,
  standalone MCP CLI, docs, dependencies, acceptance tests and real service verification.
- New configbackend defaults local; cairn_mcp selected explicitly. Runtime BuildRetriever returns
  agent.Retriever and only builds selected backend. MCP auth token environment only by default.
- Agent Config gets opt-in RequireRAG; CLI `-rag` uses it so retrieval errors cannot masquerade as
  successful RAG. Production cairn_mcp uses strict failure initially; local behavior retained.
- Test sequence: adapterunit+oldlocalregression, standalone MCP smoke, CLI against deterministic
  LLM server and actual disposable Cairn, real configured LLM only with disposable content, then
  real WebSocket transport through production SessionHandler. No user document sent to external LLM.
- A hardcoded existing LLM credential was noticed in source; do not print it, copy it into docs,
  fixtures or commit output. Don't change unrelated credentials in this task; report rotation advice.
- Preserve user's running Cairn/knowledge data. Use isolated Compose/project for fixtures and
  stop/restart testing. Do not run full Agent service against production business dependencies.

## Execution journal

- A02: Baseline config/rag/agent-chat suites passed; the pre-existing unconditionally enabled real
  LLM test failed with remote EOF before implementation. Agent repository's local `replace` target
  `../shop-cluster-common` exists but lacks `transport`, so the top-level production entrypoint cannot
  build as checked out. Session/ws packages compile. These are baseline constraints, not green gates.
- A03: Parent config/strict-RAG/prompt tests first failed with missingfields/behavior, then passed.
  MCP config is opt-in, keys environment-only, strict retrieval failures stop before LLM with a safe
  message. Selected SDK raised Go floor1.25.0; local compiler1.25.4 verified. No external credentials
  changed. Sole Hooke owns only adapter+tests; parent owns remaining code and dependencies.
- A04: Parent factory tests reproduced old unconditional localJSONL initialization, then passed with
  explicit backend selection. Standalone MCP CLI compiles. Deterministic config/rag/agent/agent-chat
  suites pass with only the named pre-existing live-LLM test excluded. Broader session baseline tests
  panic because their existing setup omits loggerutil initialization; no unrelated production fix.
  New WebSocket acceptance initializes its required logger locally and exercises production handler.
- A05: Docker engine was stopped and installation had moved from C: toD:/Program/Docker. Started
  actual Desktop executable with hidden window, confirmed engine29.7.2, now creating isolated
  cairn-agent-acceptance project (HTTP38883, MCP39081..39090). Old desktop-start CLI attempts did not
  produce a working daemon. No user container/data deletion. Configured LLM/models unauthenticated
  probes both with/without proxy returned RemoteProtocolError; no TLS/security bypass attempted.

- A06: Sole sol/high adapter worker Hooke delivered and was closed. Parent reviewed schema/auth,
  cancellation-aware concurrency and reconnect behavior, independently ran deterministic regression,
  race and vet checks. Official Go SDK1.6.1 is a direct pinned dependency; Go floor1.25.0 documented.
  Existing Retriever contract and default local backend retained. Cairn mode skips local JSONL,
  dictionary, Qdrant and old embedding construction; prompt references are quoted untrusted data.
  MCP transport/tool errors are sanitized, no unauthorized retries, API keys never enter prompts.
  Canonical KB/document/chunk IDs and request_id survive mapping; runtime logs counts/duration,
  not retrieved content or credentials. Automatic request_id log correlation is not implemented.
- A07: Independent production build succeeded using an ignored alternate modfile with the matching
  published common module instead of the stale local replacement. The repository's replacement
  remains unchanged. Published transport handles graceful shutdown and returns to main; deferred
  router.Shutdown closes the MCP session. Default root build remains blocked by the local checkout.
- A08: Isolated Compose cairn-agent-acceptance uses real PostgreSQL/pgvector, Redis, TEI and all five
  workers. A unique synthetic document was uploaded and indexed via public management APIs. The
  official Go MCP CLI initialized/discovered/searched; Agent CLI passed the unique retrieved fact
  to a deterministic HTTP/SSE chat endpoint and returned it. Real WebSocket transport through
  production SessionHandler also returned the fact and agent_chat_done. No production device auth,
  business database, speech or TTS integration was replaced or claimed accepted by this fixture.
- A09: Stopping the managed MCP service made strict CLI fail before any new model request. Starting
  it restored retrieval. A separate live test retained ONE retriever instance through stop/start
  and successfully recovered without restarting the Agent. Temporary query keys revoked in finally;
  no live credential stored in reports. The script additionally guarantees local HTTP server cleanup
  on early failures and attempts key revocation even if restoring MCP state fails.
- A10: Final repeat of selected Go regression/vet and complete published-module executable build
  passed. Ruff check/format passed for the reproducible acceptance script. Its narrow S603 exception
  permits only reviewed fixed argument-list subprocess calls without a shell or user command input.
  Prior acceptance success is not external LLM success: the existing real-model test had remote EOF
  before our changes, and real configured model availability remains an explicit release gate.
- A11: Final complete six-stage live acceptance rerun passed after script cleanup hardening, including
  same-client restart; temporary query key revoked. Final race run passed all five selected packages
  (rag70.067s). Verified all13 owned container names/project labels, then compose down without -v:
  no cairn-agent-acceptance container remains. Objects/PG/Redis named volumes, model weights, images
  and ignored local evidence are retained. No default Cairn or M08 container was removed. Docker
  Desktop was started, but historical user containers were not visible in that engine; this task
  does not assert the user's normal deployment is running or silently recreate its database.

## Reproduction and handoff

### September18 local commit and dev comparison

User subsequently authorized committing Agent changes on rag and checking dev conflicts, not pushing.
Fetched origin/dev at012b099; origin/rag remained662972d. Five selected deterministic Go packages and
go vet passed again (live LLM test explicitly excluded as documented). Initial signed commit failed
because the GPG agent was unavailable; listing local signing keys restarted its agent, and retrying
the normal signed commit succeeded without disabling signing or changing Git configuration.

Agent commit: bbb194b, `feat(rag): integrate Cairn MCP retrieval backend`,18files,1927insertions,
19deletions. Agent tracked/untracked worktree is clean; rag is one commit ahead of origin/rag.
No push, deployment, branch checkout or actual merge occurred. This supersedes earlier no-commit
statements, which describe the implementation acceptance checkpoint before user authorization.

`git merge-tree --write-tree --name-only HEAD origin/dev` returned1 with exactly one conflicted file:
go.mod. The conflict is around MCP's added google/jsonschema-go and dev moving google/uuid from
indirect to direct requirements. Proposed eventual resolution: retain jsonschema-go, keep uuid only
as a direct requirement, preserve the MCP SDK and its Go1.25 floor; retain dev's newer common module
v1.2.709-dev and disabled local replacement, then rebuild/test the actual merged result. router/init.go
auto-merged, but text mergeability is not runtime compatibility evidence. Dev also changed default
LLM configuration and many business/session files; no credentials from those changes were printed.
The resulting virtual merge tree is01b085d3a9eb5095230552db2f67bfd5338265eb, not a branch commit.

Agent configuration and usage live in `D:/code/ai-smart-screen-digital-human/doc/mcp-integration.md`.
Changed Agent branch is `rag`; Cairn remains on `dev`. No commit/push/new branch; preserve both trees.
No API key or user document was copied between repositories. Existing source-held LLM credentials
should be rotated and removed by their owner; they were not copied into this implementation.

Run deterministic checks in the Agent repository:

```powershell
go test ./config ./internal/rag ./internal/agent ./test/agent-chat ./test/mcp-smoke -skip TestHandleAgentRequestWithRealLLM -count=1
go test -race ./config ./internal/rag ./internal/agent ./test/agent-chat ./test/mcp-smoke -skip TestHandleAgentRequestWithRealLLM -count=1
go vet ./config ./internal/rag ./internal/agent ./test/agent-chat ./test/mcp-smoke
```

The two live acceptance tests intentionally skip without CAIRN_AGENT_ACCEPTANCE=1; a skipped run is
not live evidence. `scripts/agent_mcp_acceptance.py` configures that environment, creates a disposable
KB, supplies a temporary key and runs both live cases. It deliberately targets only localhost38883
and the adjacent Agent checkout, not the user's default port8080 deployment. Local evidence is under
ignored `data/acceptance-agent-20260918/`: six stage logs and summary.json; no key is retained.

```powershell
docker compose --env-file data/acceptance-agent-20260918/compose.env -p cairn-agent-acceptance --profile local-model up -d
$env:CAIRN_SMOKE_PASSWORD = 'Agent-Acceptance-Verified-9'
uv run python scripts/agent_mcp_acceptance.py
```

These commands reuse THIS machine's retained isolated volumes/env/model assets, not a portable fresh
installation recipe. For first creation use the initial password from the isolated compose.env;
the script completes initial setup and changes it to the test-only value shown above. Never point
this destructive lifecycle fixture at user data. It stops/restarts MCP and adds disposable documents.

Remaining gates: reachable configured external chat model with a unique KB fact, matching local
common-module checkout for default root builds, authenticated full-business WS deployment, and
cross-machine LAN/firewall verification. Historical session tests still have an unrelated nil-logger
setup failure. Original full KB E2E/PDF/OCR quality gates are unchanged by this Agent integration.

### Generic Agent naming follow-up

User requested that the Agent repository and its published history use only generic MCP naming.
The implementation is renamed consistently: RAG_BACKEND=mcp; MCP_URL, MCP_API_KEY, MCP_KB_ID,
MCP_TIMEOUT; exported MCPConfig/MCPRetriever; mcp.go, mcp_test.go and doc/mcp-integration.md.
No legacy environment aliases are retained because they would keep product naming in Agent source.
The tool/search payload and response contract are unchanged; this is not support for arbitrary MCP
tools. The external acceptance orchestrator now passes the matching MCP_* environment and new WS
test name. Historical evidence above retains its original names to distinguish actual prior runs.
Live service tests are not repeated for this mechanical rename; their fixtures compile and skip
without explicit live opt-in. Focused Go regression/vet and script Ruff checks pass after renaming.
Only the unpushed local commit is amended; no remote rewrite, push, actual merge or deployment.

Completed rename verification: five-package regression and race suites passed, go vet passed;
the renamed opt-in WS test compiles/skips without a live fixture. Signed amend produced9b95ef0,
`feat(rag): integrate MCP retrieval backend`, replacingbbb194b in the rag branch history. Agent
worktree is clean and one commit ahead of origin/rag. Case-insensitive scans of working files,
staged content, all reachable commit messages, historical diffs and reachable paths returned no
old product name or reported misspelling. Local recovery reflogs/unreachable objects were not pruned;
they are not branch history and are not transferred by a normal push. Dev pre-merge still reports
only go.mod conflict, with router/init.go auto-merging; no merge was actually applied.

### Authorized rag push

User clarified that only dev auto-deploys and explicitly authorized pushing rag. Verified a clean
Agent worktree on rag at9b95ef0, then used a normal non-force push with the explicit refspec
refs/heads/rag:refs/heads/rag. Remote rag advanced from662972d to
9b95ef0718ca0681d932d85052d1b2c7aa26b027; ls-remote confirmed the exact local/remote match.
Remote dev remained012b099cfbb092a46964597ee94a58ab77ea2cc5 before and after the push. No merge,
dev push, PR creation or deployment action was performed. This supersedes earlier no-push checkpoints.

### Authorized dev merge and push

User explicitly authorized merging rag into dev and pushing dev for automatic server deployment.
Fetched latest refs, verified clean worktree, switched existing local dev and fast-forwarded it to
origin/dev012b099. Merged origin/rag9b95ef0 with --no-ff --no-commit. Resolved the only conflict in
go.mod: retain jsonschema-go, keep uuid only direct, keep MCP SDK1.6.1 and Go1.25 floor, preserve
dev's common module v1.2.709-dev and disabled local replacement. Existing dev LLM/config YAML are
unchanged. Additional local-RAG comment changes are inherited from rag662972d, not new merge logic.

Initial build needed missing ignored go.sum entries for published common-module transitive deps;
go build -mod=mod ./... resolved them. Subsequent ordinary go build ./... passed. Five focused Go
packages passed with only the documented external LLM test excluded; go vet including router passed.
Opt-in WS fixture compiled/skipped without live configuration, not claimed live evidence. Production
Linux amd64 build with CGO_ENABLED=0 passed using actual merged go.mod, no alternate replacement.
This resolves the prior default-root-build missing-transport blocker on dev. The Agent documentation
now explains service-process MCP/LLM environment, Go1.25 and fresh-checkout dependency resolution.

Signed merge commit c6c9b40e71b9909136cb76bc32af8d1d6820fe50 has parents012b099 and9b95ef0,
message `Merge rag into dev: integrate MCP retrieval backend`. Normal explicit-refspec push to dev
succeeded; ls-remote confirms origin/dev matches exactly, origin/rag remains9b95ef0. Agent worktree
clean on dev. No force push, no model credential modification, no test containers started this turn.
Server pipeline/restart and real-model operation are not observable from this Git result and remain
to be verified by the user. MCP is opt-in: deploy RAG_BACKEND=mcp and reachable MCP_* configuration.

### September20 remote deployment diagnosis

User supplied read-only Kubernetes observations from namespace shop-cluster. Deployment image is
3.0.20260920140004; new Pod596bdbc49c-vbp4f is CrashLoopBackOff, old Pod5c9565b6bc-wckbf is1/1 Running.
Deployment env lists only the existing logging-related variable and RAG_BACKEND, with no envFrom.
Previous-container logs at14:16:46+08 confirm the fatal panic in router.initRAGRetriever/router.Init,
matching configured RAG backend initialization failure; stack arguments show empty MCP API key.
This matches ValidateRetriever rejecting MCP mode without MCP_API_KEY, before network discovery.
The required endpoint/key/KB environment has not been supplied. No server mutation was performed by
the coordinator. Preserve the old healthy Pod and do not apply the stale June-image local manifest.
Next step: obtain the server-reachable MCP endpoint, provision a scoped key via Secret and inject
complete configuration together, then check rollout and a real knowledge query. No key is recorded.
Earlier Redis initialization logged a recovered nil-pointer error and the app proceeded to router
startup; it is separate from this fatal stack and must not be declared healthy without verification.
Logs also include credential-bearing configuration; ask for redaction and credential rotation if
actual secrets were exposed. No credential values are copied into this journal.

### October12 remote-head and rollback-target check

User asked whether the last submitted change is still latest and how to roll back. Read-only
ls-remote confirms remote dev remainsc6c9b40e71b9909136cb76bc32af8d1d6820fe50 and rag remains
9b95ef0718ca0681d932d85052d1b2c7aa26b027. Agent worktree is clean on dev. The merge's first parent
is012b099cfbb092a46964597ee94a58ab77ea2cc5; this is the repository target before MCP integration.
Reviewed first-parent reverse diff:28files, reverting MCP integration and inherited rag changes;
dev's existing common module1.2.709-dev is retained by a complete revert. No newer remote dev
commit is present as of this check. A normal git revert -m1 ofc6c9b40 creates an auditable reversal
and avoids rewriting shared history; it must be built/tested before an authorized dev push.
For immediate cluster rollback, first inspect current Deployment revisions and old healthy Pod's
image; a commit SHA or timestamped image tag alone cannot identify the correct Kubernetes revision.
Rollback changes the Pod template, not the external mounted ConfigMap/Secret data, and an automatic
deployment controller may later restore the repository's desired version. No rollback, commit,
push or server mutation has been performed during this read-only inquiry.
