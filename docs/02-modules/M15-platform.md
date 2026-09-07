# M15 — Platform Services (Audit, Settings, Quotas, Usage)

| | |
| --- | --- |
| **Package** | `cairn.platform` |
| **Layer** | L3 control plane |
| **Phase** | 1 (audit, settings) · 3 (quotas, usage) · 5 (notifications) |
| **Owner** | Backend lead |
| **Depends on** | M00, M06 |
| **Depended on by** | every module that mutates state or spends resources |
| **Tables owned** | `workspace`, `audit_log`, `usage_record` |
| **Requirements owned** | FR-O-01..08 |

---

## 1. Purpose and scope

The cross-cutting services every other module needs: an audit trail, workspace settings, quota
enforcement, usage accounting, and notifications.

**In scope:** append-only audit logging and querying, workspace settings with validated schemas,
quota definition and enforcement, usage aggregation, notification delivery.

**Out of scope:** authorization decisions (M02 — this module *records* them), billing.

---

## 2. Public interface

```python
class AuditService:
    async def record(self, session: AsyncSession | None = None, *,
                     action: str, outcome: Literal["success","failure","denied"] = "success",
                     actor_id: UUID | None = None, actor_type: str = "user",
                     resource_type: str | None = None, resource_id: UUID | None = None,
                     before: dict | None = None, after: dict | None = None,
                     detail: dict | None = None) -> None:
        """Pass `session` to record in the caller's transaction (the default for state changes).
        Omit it for fire-and-forget events such as denials."""
    async def query(self, workspace_id, filters: AuditFilters, page) -> CursorPage[AuditEntry]: ...
    async def export(self, workspace_id, filters, fmt: Literal["jsonl","csv"]) -> AsyncIterator[bytes]: ...

class SettingsService:
    async def get(self, workspace_id) -> WorkspaceSettings: ...
    async def update(self, actor, workspace_id, patch: dict) -> WorkspaceSettings: ...
    def get_effective(self, workspace_id) -> WorkspaceSettings: ...    # cached, sync

class QuotaService:
    async def check(self, workspace_id, kind: QuotaKind, amount: int = 1) -> None:
        """Raises QuotaExceededError. Called BEFORE the operation."""
    async def consume(self, workspace_id, kind: QuotaKind, amount: int) -> None: ...
    async def usage(self, workspace_id) -> QuotaUsage: ...

class UsageService:
    async def record(self, spec: UsageSpec) -> None:      # buffered, async flush
    async def summary(self, workspace_id, group_by, period) -> UsageSummary: ...

class NotificationService:
    async def send(self, workspace_id, event: NotificationEvent) -> None: ...
```

---

## 3. Behaviour

### 3.1 Audit logging (`FR-O-01`)

**Recorded actions** — the list is normative, not illustrative:

```
system.bootstrap
user.create · user.update · user.delete · user.activate · user.deactivate
user.credentials.change · user.credentials.initial_setup · user.credentials.force_change
auth.login · auth.login.failed · auth.logout · auth.locked
grant.create · grant.revoke · break_glass.open · break_glass.expire
apikey.create · apikey.revoke
kb.create · kb.update · kb.delete · kb.reindex · kb.transfer
document.delete · chunk.edit
provider.create · provider.update · provider.delete · secret.rotate
storage_binding.create · storage_binding.update · storage_binding.delete
function.publish · function.egress_approve
pipeline.publish
settings.update · quota.update
authz.denied
```

Properties:
- **Append-only.** No `UPDATE` or `DELETE` path exists in the repository. Enforced by a
  database-level rule and by there being no method to call.
- **Transactional where it matters.** State-change audits commit with the change, so a rolled-
  back operation leaves no misleading audit entry, and a committed one is never missing its record.
- `actor_label` is **denormalized** — the audit trail must remain readable after the actor is
  deleted, which is precisely when it is most often needed.
- `before`/`after` capture the changed fields only, with secret-shaped values redacted.
- Partitioned monthly; retention by partition drop (`FR-O-03`).

### 3.2 Workspace settings (`FR-O-06`)

Validated `WorkspaceSettings` (see [data model §3.1](../01-architecture/03-data-model.md)).
Cached in-process with a 30 s TTL and invalidated on update. Some settings are hot-reloadable
(rate limits, quotas, `admin_content_access`); others require a restart and are marked as such
in the UI rather than appearing to apply and not doing so.

### 3.3 Quotas (`FR-O-04`, `FR-O-05`)

| Quota | Enforced at | On breach |
| --- | --- | --- |
| `documents` | upload registration | Reject with the current and limit values |
| `storage_bytes` | upload registration | Reject |
| `vectors` | index stage | **Pause the KB's ingestion, do not corrupt** |
| `embedding_tokens_monthly` | embed stage | Pause ingestion; retrieval continues |
| `retrieval_rpm` | data plane | 429 with `Retry-After` |
| `knowledge_bases` | KB creation | Reject |

Checked **before** the operation, so a breach never leaves half-written state (`FR-O-05`).
Ingestion pause is a task-level `blocked` state, resumable once quota is raised — not a failure
that loses the work.

Counters live in Redis for hot paths with periodic reconciliation against the database, since a
per-request `SELECT count(*)` on the data plane would violate the latency budget.

### 3.4 Usage accounting (`FR-O-07`)

Buffered in memory and flushed every 5 s or 1000 records, whichever comes first, with a flush on
shutdown. Never on the request's critical path. Aggregated by workspace, KB, principal, model,
and kind; queryable by period.

### 3.5 Notifications (`FR-O-08`)

Channels: email (SMTP) and webhook (HMAC-signed). Events: ingestion failure batch, quota
threshold at 80%/100%, break-glass opened, crawl run failed, reindex completed or failed.

Delivery is a `maintain` task with retries. Webhook targets go through
`core.http.safe_client()` — a user-supplied webhook URL is an SSRF vector like any other
(`NFR-SEC-09`).

---

## 4. API endpoints owned

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/v1/audit-log` | `platform:audit` |
| GET | `/v1/audit-log/export` | `platform:audit` |
| GET/PATCH | `/v1/settings` | `platform:settings` |
| GET/PATCH | `/v1/quotas` | `platform:settings` |
| GET | `/v1/usage` | `platform:audit` or self-scoped |
| GET/POST | `/v1/notifications/channels` | `platform:settings` |

---

## 5. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M15-01 | Every listed action produces an audit entry | FR-O-01 |
| TC-M15-02 | **Audit entries have no update or delete path** | FR-O-01 |
| TC-M15-03 | Transactional audit rolls back with a failed operation | §3.1 |
| TC-M15-04 | `actor_label` survives actor deletion | §3.1 |
| TC-M15-05 | Secrets are redacted from `before`/`after` | NFR-SEC-03 |
| TC-M15-06 | Audit query filters by actor, action, resource, and time | FR-O-02 |
| TC-M15-07 | JSONL export streams without loading everything into memory | FR-O-02 |
| TC-M15-08 | Retention drops partitions past the window | FR-O-03 |
| TC-M15-09 | Each quota is enforced at the specified point | FR-O-04 |
| TC-M15-10 | **Quota breach leaves no partial state** | FR-O-05 |
| TC-M15-11 | Vector-quota breach pauses ingestion resumably | §3.3 |
| TC-M15-12 | Settings update invalidates the cache within the TTL | FR-O-06 |
| TC-M15-13 | Usage records buffer and flush; nothing lost on shutdown | FR-O-07 |
| TC-M15-14 | Usage accounting adds < 1 ms to the caller | §3.4 |
| TC-M15-15 | Webhook delivery is HMAC-signed and SSRF-guarded | FR-O-08 |

Coverage target: **85%**.

---

## 6. Task breakdown

| Task | Description | Est (d) | Phase |
| --- | --- | --- | --- |
| T-M15-01 | ORM + migration: `workspace`, `audit_log` (partitioned), `usage_record` | 1.0 | 1 |
| T-M15-02 | Audit service: record, transactional mode, redaction | 1.5 | 1 |
| T-M15-03 | Audit query + streaming export | 1.0 | 1 |
| T-M15-04 | Partition management (create ahead, drop expired) | 1.0 | 1 |
| T-M15-05 | Settings service + schema + caching | 1.0 | 1 |
| T-M15-06 | Quota service: definitions, checks, Redis counters, reconciliation | 2.0 | 3 |
| T-M15-07 | Ingestion pause/resume on quota breach | 1.0 | 3 |
| T-M15-08 | Usage service with buffered flush | 1.0 | 3 |
| T-M15-09 | Usage aggregation queries | 1.0 | 3 |
| T-M15-10 | Notification channels + delivery task | 2.0 | 5 |
| T-M15-11 | Routers | 1.0 | 1–3 |
| T-M15-12 | Tests TC-M15-01..15 | 1.5 | 1–3 |
| | **Total** | **15.0** | |
