# API Conventions

**Document:** `03-api/01-api-conventions.md`
**Status:** Normative — the public contract
**Date:** 2026-08-28

The API is the product. These conventions are contract terms, not style preferences.

---

## 1. Versioning and stability

| Rule | Detail |
| --- | --- |
| Path versioning | `/v1/...`. The version is the major version only. |
| Additive evolution | New optional request fields and new response fields may be added within `/v1` at any time (`FR-I-05`). |
| Client obligation | Clients MUST ignore unknown response fields. Documented explicitly in the SDK docs. |
| Breaking changes | Require `/v2`, a `Sunset` header on `/v1`, and **≥ 6 months** notice. |
| Deprecation | `Deprecation: true` and `Sunset: <RFC 9110 date>` headers; `Link: <docs>; rel="deprecation"`. |
| Never | Change the meaning of an existing field, change an error `code`, remove a field, or tighten validation on an existing field within a major version. |

**Preview features** are marked `X-Cairn-Preview: <feature>` in the response and documented as
subject to change. Nothing is preview by default.

---

## 2. Authentication

| Client | Mechanism |
| --- | --- |
| External Agent / application | `Authorization: Bearer cairn_sk_live_...` |
| Web UI | Opaque session token in an `HttpOnly; Secure; SameSite=Lax` cookie + CSRF token |
| MCP client | `Authorization: Bearer cairn_sk_live_...` (identical to API keys) |

The data plane accepts **only** API keys. The control plane accepts both.

Unauthenticated endpoints, exhaustively: `GET /healthz`, `GET /readyz`, `GET /v1/meta`,
`POST /v1/auth/login`, `GET /v1/openapi.json`.

---

## 3. Request conventions

| Convention | Rule |
| --- | --- |
| Content type | `application/json; charset=utf-8`. Multipart only for file upload. |
| Field naming | `snake_case` |
| IDs | Prefixed public IDs (`kb_01H…`). Raw UUIDs are rejected. |
| Unknown fields | **Rejected** with `VALIDATION_FAILED` on write endpoints (`NFR-SEC-05`) |
| Timestamps | RFC 3339 with `Z` — `2026-08-28T14:31:07Z` |
| Durations | Integers with the unit in the field name — `ttl_seconds`, `latency_ms` |
| Enums | Lowercase snake_case strings, never integers |
| Nulls | `null` means "explicitly unset"; omission means "unchanged" on PATCH |
| Max body | 1 MB for JSON, 200 MB for upload (configurable) |

### Standard request headers

| Header | Required | Purpose |
| --- | --- | --- |
| `Authorization` | yes | Bearer token |
| `Content-Type` | on write | |
| `X-Request-Id` | no | Client correlation ID; echoed back. Generated if absent. |
| `Idempotency-Key` | recommended on POST | (`FR-I-10`) |
| `Accept-Language` | no | Error message localization |

---

## 4. Response conventions

### Standard response headers

| Header | Always | Purpose |
| --- | --- | --- |
| `X-Request-Id` | yes | Correlation (`FR-I-08`) |
| `RateLimit-Limit` / `-Remaining` / `-Reset` | yes | RFC 9331 (`FR-I-07`) |
| `Retry-After` | on 429/503 | Seconds |
| `Deprecation` / `Sunset` | when applicable | |

### Success codes

| Code | Use |
| --- | --- |
| 200 | Successful GET, PATCH, POST returning a result |
| 201 | Resource created; `Location` header set |
| 202 | Accepted for async processing; body contains a job reference |
| 204 | Successful DELETE with no body |

**A retrieval returning zero results is a `200` with an empty `results` array**, never a 404
(`FR-H-07`). "Nothing matched" is a valid answer, not an error.

---

## 5. Errors — RFC 9457 (`FR-I-04`)

`Content-Type: application/problem+json`

```json
{
  "type": "https://docs.cairn.io/errors/validation-failed",
  "title": "Validation failed",
  "status": 400,
  "code": "VALIDATION_FAILED",
  "detail": "The request contains invalid parameters.",
  "instance": "/v1/retrieval/query",
  "request_id": "req_01HQZX3N9K2M5P7R8T",
  "errors": [
    { "field": "top_k", "code": "OUT_OF_RANGE",
      "detail": "must be between 1 and 100", "value": 500 },
    { "field": "targets", "code": "REQUIRED", "detail": "at least one target is required" }
  ]
}
```

Rules:
- **`code` is the contract.** Clients branch on it. It never changes after release.
- `detail` is human-facing, may be localized, and may change. Never contains secrets, SQL, stack
  traces, or internal hostnames.
- `errors[]` appears only for field-level validation failures.
- 5xx bodies carry only a generic `detail` plus `request_id`; the real detail is logged.

---

## 6. Pagination (`FR-I-11`)

Cursor-based, always. Offset pagination is not offered.

```
GET /v1/knowledge-bases/kb_01H…/documents?limit=50&cursor=eyJrIjpb…
```

```json
{
  "items": [ … ],
  "next_cursor": "eyJrIjpbIjIwMjYtMDgtMjhUMTQ6MzE6MDdaIiwiZG9jXzAx…",
  "has_more": true
}
```

| Rule | Detail |
| --- | --- |
| `limit` | default 50, max 200 |
| `cursor` | opaque base64url; clients MUST NOT parse it |
| Stability | Sorted by `(sort_field, id)` so results do not shift under concurrent writes |
| End of results | `next_cursor: null`, `has_more: false` |
| Expiry | Cursors remain valid ≥ 24 h |

> Offset pagination is excluded deliberately: it silently skips or duplicates rows when the
> underlying set changes between pages, and it degrades linearly on deep pages. For a document
> list being actively ingested, both problems are immediate.

---

## 7. Registered error codes

Codes are permanent. New ones may be added; existing ones never change meaning.

| Code | HTTP | Module |
| --- | --- | --- |
| `VALIDATION_FAILED` | 400 | M00 |
| `INVALID_ID` | 400 | M00 |
| `BLOCKED_ADDRESS` | 400 | M00 |
| `AUTHENTICATION_FAILED` | 401 | M01 |
| `SESSION_EXPIRED` | 401 | M01 |
| `SETUP_TOKEN_INVALID` | 401 | M01 |
| `API_KEY_INVALID` | 401 | M02 |
| `PERMISSION_DENIED` | 403 | M02 |
| `PASSWORD_CHANGE_REQUIRED` | 403 | M01 |
| `CONTENT_ACCESS_REQUIRES_GRANT` | 403 | M02 |
| `API_KEY_IP_NOT_ALLOWED` | 403 | M02 |
| `FUNCTION_EGRESS_DENIED` | 403 | M12 |
| `RESOURCE_NOT_FOUND` | 404 | M00 |
| `RESOURCE_CONFLICT` | 409 | M00 |
| `REQUEST_IN_PROGRESS` | 409 | M00 |
| `USERNAME_TAKEN` | 409 | M01 |
| `LAST_ADMIN_PROTECTED` | 409 | M01 |
| `EMBEDDING_MODEL_IMMUTABLE` | 409 | M03 |
| `KB_INDEX_IN_PROGRESS` | 409 | M03 |
| `KB_NOT_READY` | 409 | M03 |
| `CHUNK_NOT_EDITABLE` | 409 | M03 |
| `FILE_TOO_LARGE` | 413 | M03 |
| `UNSUPPORTED_FILE_TYPE` | 400 | M03 |
| `PASSWORD_POLICY_VIOLATION` | 400 | M01 |
| `PASSWORD_REUSED` | 400 | M01 |
| `GRANT_EXCEEDS_OWNER` | 400 | M02 |
| `BREAK_GLASS_REASON_REQUIRED` | 400 | M02 |
| `MODEL_CAPABILITY_MISMATCH` | 400 | M10 |
| `MODEL_DIMENSION_MISMATCH` | 400 | M10 |
| `MODEL_DISABLED` | 400 | M10 |
| `EMBEDDING_DIMENSION_MISMATCH` | 400 | M05 |
| `PIPELINE_CYCLE_DETECTED` | 400 | M11 |
| `PIPELINE_TYPE_MISMATCH` | 400 | M11 |
| `PIPELINE_PORT_UNBOUND` | 400 | M11 |
| `PIPELINE_FUNCTION_INVALID` | 400 | M11 |
| `PIPELINE_MODEL_INVALID` | 400 | M11 |
| `FUNCTION_SYNTAX_ERROR` | 400 | M12 |
| `FUNCTION_SIGNATURE_MISMATCH` | 400 | M12 |
| `FUNCTION_DEPENDENCY_NOT_ALLOWED` | 400 | M12 |
| `FUNCTION_OUTPUT_INVALID` | 400 | M12 |
| `IDEMPOTENCY_KEY_REUSED` | 422 | M00 |
| `RATE_LIMIT_EXCEEDED` | 429 | M02 |
| `QUOTA_EXCEEDED` | 429 | M15 |
| `INTERNAL_ERROR` | 500 | M00 |
| `UPSTREAM_UNAVAILABLE` | 502 | M00 |
| `PROVIDER_UNREACHABLE` | 502 | M10 |
| `PROVIDER_AUTH_FAILED` | 502 | M10 |
| `STORAGE_BINDING_UNAVAILABLE` | 502 | M03 |
| `PROVIDER_CIRCUIT_OPEN` | 503 | M10 |
| `SANDBOX_UNAVAILABLE` | 503 | M12 |

---

## 8. Idempotency (`FR-I-10`)

`Idempotency-Key: <opaque, ≤ 255 chars>` on POST endpoints that create resources.

| Situation | Response |
| --- | --- |
| First use | Processed; response stored for 24 h |
| Replay, identical body | Stored response returned, plus `X-Idempotent-Replay: true` |
| Replay while in progress | `409 REQUEST_IN_PROGRESS` |
| Same key, different body | `422 IDEMPOTENCY_KEY_REUSED` |

The body fingerprint check matters: without it, reusing a key with a different payload silently
returns the wrong resource, which is far worse than an error.

---

## 9. Rate limiting (`FR-I-07`)

Headers on **every** response, including errors:

```
RateLimit-Limit: 600
RateLimit-Remaining: 417
RateLimit-Reset: 23
```

On 429, `Retry-After: 23` is also set. Limits are per API key; a per-IP limit applies
additionally to unauthenticated endpoints.

> A machine client that can see its own budget throttles itself. One that cannot, retries into a
> wall and takes the service down with it.

---

## 10. Streaming

Server-Sent Events, not WebSocket, for chat, pipeline test runs, and long job progress.

```
Content-Type: text/event-stream

event: chunk
data: {"delta":"Key rotation"}

event: done
data: {"usage":{"tokens":142}}
```

SSE is chosen because it survives proxies, reconnects natively with `Last-Event-Id`, requires no
protocol upgrade, and is trivially testable with `curl`.

---

## 11. OpenAPI and SDKs

- `GET /v1/openapi.json` — OpenAPI 3.1, generated, always current (`FR-I-06`).
- Every endpoint has `summary`, `description`, and at least one working example. **The
  generated spec is the SDK documentation** — writing it carelessly means shipping bad docs.
- SDKs generated in CI and published: `cairn` (Python), `@cairn/client` (TypeScript) (`FR-I-09`).
- A spec change without a corresponding SDK regeneration fails the build.
