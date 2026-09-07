# M12 — Function Library and Sandbox

| | |
| --- | --- |
| **Package** | `cairn.functions` |
| **Layer** | L3 control plane + worker |
| **Phase** | 4 |
| **Owner** | Backend eng. D (with a mandatory security review) |
| **Depends on** | M00, M02 (facade), M04, M06, M15 (facade) |
| **Depended on by** | M07, M11 |
| **Tables owned** | `function`, `function_version` |
| **Requirements owned** | FR-M-01..09, NFR-SEC-06 |

---

## 1. Purpose and scope

User-supplied Python transforms that plug into typed pipeline slots, executed inside a real
isolation boundary.

> ### ⚠️ This module is remote code execution by design.
>
> `RestrictedPython`, AST allowlists, and `__builtins__` stripping are **speed bumps, not
> security boundaries** — every one of them has published bypasses. If the kernel-level sandbox
> is not ready, **do not ship this feature**. This is the one item in the plan worth holding a
> release for. A security review sign-off is a hard gate on `T-M12-13`.

**In scope:** function authoring, slot signatures, versioning, the sandbox runner service,
resource limits, egress control, dependency allowlisting, testing.

**Out of scope:** the pipeline graph (M11), arbitrary agent tools (out of product scope per
ADR-0008).

---

## 2. Slot signatures (`FR-M-02`)

Exactly five slots. Each has a fixed, statically checked contract.

```python
# slot: parse
def run(data: bytes, ctx: ParseContext) -> ParsedDocument: ...

# slot: chunk
def run(doc: ParsedDocument, cfg: ChunkConfig) -> list[Chunk]: ...

# slot: enrich
def run(chunk: Chunk, ctx: EnrichContext) -> Chunk: ...

# slot: filter
def run(hits: list[Hit], ctx: FilterContext) -> list[Hit]: ...

# slot: rerank
def run(query: str, hits: list[Hit]) -> list[Hit]: ...
```

Fixed signatures are what make functions testable, composable, and statically validatable at
pipeline-publish time — none of which is possible with arbitrary tools.

---

## 3. Sandbox architecture (`FR-M-03`)

```
   worker-parse / api-control                  sandbox-runner (separate container)
   ┌────────────────────────┐   gRPC/HTTP     ┌──────────────────────────────────┐
   │ SandboxClient          │────────────────▶│ dispatcher                        │
   │  execute(fn_ver, input)│                 │   ├─ spawn runsc container        │
   │  timeout, limits       │◀────────────────│   ├─ mount code read-only         │
   └────────────────────────┘   result/error  │   ├─ pipe input on stdin          │
                                              │   ├─ enforce cgroup limits        │
                                              │   ├─ collect stdout/result        │
        NO shared filesystem                  │   └─ DESTROY container            │
        NO shared network namespace            └──────────────────────────────────┘
```

### Mandatory controls (`FR-M-04`, `FR-M-05`, `NFR-SEC-06`)

| Control | Setting | Why |
| --- | --- | --- |
| Runtime | **gVisor (`runsc`)**, or Firecracker microVM | A real kernel boundary. Not namespaces alone. |
| Network | **`--network=none` by default** | An escape without egress cannot exfiltrate. Per-function allowlist requires admin approval and routes through an egress proxy. |
| Filesystem | Read-only rootfs; `tmpfs` `/tmp` capped at 64 MB | No persistence, no host writes |
| User | Non-root, UID 65534, `--cap-drop=ALL`, `no-new-privileges` | |
| Seccomp | Default gVisor profile | |
| CPU | 1 core, cgroup quota | |
| Memory | 512 MB default, hard limit | OOM kills the container, not the host |
| PIDs | 64 | Fork-bomb containment |
| Wall clock | 30 s default, hard kill | |
| Lifetime | **Destroyed after every single execution** | No state carries between runs or between tenants |
| Output size | 8 MB cap | |
| Concurrency | Bounded pool, default 4 | |

Warm-pool optimization is permitted **only** if each container still serves exactly one
execution before being replaced. Reusing a container across executions reintroduces
cross-tenant state and is prohibited.

### Dependency allowlist (`FR-M-08`)

The sandbox image is pre-built with an approved set (`re`, `json`, `datetime`, `math`,
`statistics`, `collections`, `itertools`, `typing`, `dataclasses`, `bs4`, `lxml`, `regex`,
`pydantic`, `numpy`, `jieba`, `markdown-it-py`). `function_version.dependencies` is validated
against it at publish time. **No runtime `pip install`** — the sandbox has no network, which
makes this structurally enforced rather than merely policy.

---

## 4. Public interface

```python
class FunctionService:
    async def create(self, actor, spec: FunctionSpec) -> FunctionView: ...
    async def save_draft(self, actor, fn_id, source, deps, limits) -> FunctionVersionView: ...
    async def publish(self, actor, fn_id, version) -> FunctionVersionView: ...
    async def test(self, actor, fn_id, version, sample: Any) -> TestResult: ...
    async def list_(self, workspace_id, slot=None) -> list[FunctionView]: ...
    async def get_version(self, fn_id, version) -> FunctionVersionView: ...

class SandboxClient:
    async def execute(self, fn: FunctionVersionRef, payload: Any,
                      *, limits: Limits) -> SandboxResult: ...
    async def health(self) -> HealthStatus: ...

@dataclass
class SandboxResult:
    ok: bool
    value: Any | None
    error: SandboxError | None
    stdout: str                # captured, truncated to 64 KB
    duration_ms: int
    peak_memory_mb: int
    exit_code: int
```

---

## 5. Behaviour

### 5.1 Publish-time validation

| Check | Failure |
| --- | --- |
| Parses as Python 3.12 | `FUNCTION_SYNTAX_ERROR` with line and column |
| Defines exactly one `run` | `FUNCTION_ENTRYPOINT_MISSING` |
| `run` signature matches the slot | `FUNCTION_SIGNATURE_MISMATCH` |
| Imports ⊆ allowlist | `FUNCTION_DEPENDENCY_NOT_ALLOWED` naming the module |
| Source ≤ 256 KB | `FUNCTION_TOO_LARGE` |
| Test execution against a sample succeeds | `FUNCTION_TEST_FAILED` |

Static analysis is a **usability** feature — catching mistakes early with good messages. It is
explicitly **not** relied upon for security; the sandbox is.

### 5.2 Failure containment (`FR-M-09`)

| Failure | Result |
| --- | --- |
| Exception in user code | `FUNCTION_RUNTIME_ERROR` + traceback (user code frames only) |
| Timeout | `FUNCTION_TIMEOUT` — terminal, no retry |
| OOM | `FUNCTION_OOM` — terminal |
| Wrong return type | `FUNCTION_OUTPUT_INVALID` — validated against the slot schema |
| Sandbox service unavailable | `SANDBOX_UNAVAILABLE` — retryable |
| Attempted network access | `FUNCTION_EGRESS_DENIED` |

The owning task fails with a clear error. Nothing else is affected — not the host, not other
tasks, not other tenants.

### 5.3 Egress allowlist

Requesting egress is an admin-approved action, recorded in the audit log. When granted, traffic
routes through an egress proxy enforcing the domain allowlist and the SSRF rules of
`NFR-SEC-09` — the function still cannot reach private address space.

---

## 6. API endpoints owned

| Method | Path | Permission |
| --- | --- | --- |
| GET/POST | `/v1/functions` | `function:use` / `function:edit` |
| GET/PATCH/DELETE | `/v1/functions/{id}` | |
| GET/POST | `/v1/functions/{id}/versions` | `function:edit` |
| POST | `/v1/functions/{id}/versions/{v}/publish` | `function:edit` |
| POST | `/v1/functions/{id}/versions/{v}/test` | `function:edit` |
| GET | `/v1/functions/slots` | authenticated |
| POST | `/v1/functions/{id}/egress-request` | `function:edit` |
| POST | `/v1/functions/{id}/egress-approve` | admin |

---

## 7. Test requirements

Security tests are **not optional** and gate the release.

| ID | Test | Req |
| --- | --- | --- |
| TC-M12-01 | A valid function of each slot executes and returns typed output | FR-M-01/02 |
| TC-M12-02 | Signature mismatch is rejected at publish | §5.1 |
| TC-M12-03 | Disallowed import is rejected with the module named | FR-M-08 |
| TC-M12-04 | **Escape attempt: `os.system('id')` fails** | NFR-SEC-06 |
| TC-M12-05 | **Escape attempt: reading `/etc/shadow` fails** | NFR-SEC-06 |
| TC-M12-06 | **Escape attempt: writing outside `/tmp` fails** | FR-M-05 |
| TC-M12-07 | **Escape attempt: outbound HTTP fails with `FUNCTION_EGRESS_DENIED`** | FR-M-04 |
| TC-M12-08 | **Escape attempt: DNS resolution fails** | FR-M-04 |
| TC-M12-09 | **Escape attempt: reaching the host Postgres fails** | FR-M-04 |
| TC-M12-10 | **Fork bomb is contained by the PID limit; host stays responsive** | FR-M-05 |
| TC-M12-11 | **Memory bomb OOMs the container only; host unaffected** | FR-M-05 |
| TC-M12-12 | **Infinite loop is killed at the wall-clock limit** | FR-M-05 |
| TC-M12-13 | **`ctypes` / `mmap` / native-code escape attempts fail** | NFR-SEC-06 |
| TC-M12-14 | **State written in run 1 is absent in run 2 (container destroyed)** | FR-M-05 |
| TC-M12-15 | **A function cannot observe another tenant's execution** | FR-M-05 |
| TC-M12-16 | Wrong return type is rejected against the slot schema | §5.2 |
| TC-M12-17 | Traceback shows user frames only, no host paths | §5.2 |
| TC-M12-18 | Sandbox unavailability is retryable; timeout is terminal | §5.2 |
| TC-M12-19 | Function failure does not affect concurrent tasks | FR-M-09 |
| TC-M12-20 | Approved egress reaches the allowed domain but not others | §5.3 |
| TC-M12-21 | Approved egress still cannot reach private address space | NFR-SEC-09 |
| TC-M12-22 | Versions are immutable; pipelines pin exact versions | FR-M-06 |
| TC-M12-23 | Test endpoint returns stdout, duration, and peak memory | FR-M-07 |

Coverage target: **85%**; **100% of TC-M12-04..15 must pass** as a release gate.

---

## 8. Acceptance criteria

- [ ] All 23 test cases pass
- [ ] **Independent security review sign-off recorded before merge**
- [ ] gVisor confirmed active in the deployed sandbox image (not silently falling back to runc)
- [ ] Sandbox container has no route to Postgres, Redis, the object store, or the vector store
- [ ] Escape-attempt suite runs in CI on every PR touching this module
- [ ] A documented runbook exists for revoking a malicious function

---

## 9. Task breakdown

| Task | Description | Est (d) |
| --- | --- | --- |
| T-M12-01 | ORM + migration: `function`, `function_version` | 0.5 |
| T-M12-02 | Slot signature definitions + Pydantic I/O schemas | 1.0 |
| T-M12-03 | Static validation (parse, signature, imports) | 1.5 |
| T-M12-04 | **Sandbox image: base, allowlisted deps, non-root** | 1.5 |
| T-M12-05 | **`sandbox-runner` service: dispatcher, runsc spawn, limits** | 3.0 |
| T-M12-06 | `SandboxClient` protocol + retries + health | 1.0 |
| T-M12-07 | Output validation against slot schemas | 0.5 |
| T-M12-08 | Error taxonomy + traceback sanitization | 1.0 |
| T-M12-09 | Versioning, publish, pin semantics | 1.0 |
| T-M12-10 | Test endpoint with resource reporting | 1.0 |
| T-M12-11 | Egress allowlist + proxy + approval flow | 2.0 |
| T-M12-12 | Routers | 1.0 |
| T-M12-13 | **Escape-attempt security suite TC-M12-04..15 + review gate** | 3.0 |
| T-M12-14 | Remaining tests | 1.5 |
| | **Total** | **19.5** |
