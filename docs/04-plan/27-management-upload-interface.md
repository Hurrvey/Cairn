# Management/upload interface contract for delivery packages B-C

Date: 2026-09-15. Status: design for existing user-approved five-part delivery; not implemented.

## Product flows

Keep current Vue/ElementPlus patterns and English/Chinese labels. Add sidebar Knowledge Bases and
administrator Model/Storage settings. KB creation must show available embedding model and object/
vector bindings instead of expecting UUID entry. Document screens display actual state, stage,
progress, errors, retry/delete/download and chunk editor. Query console uses the accepted retrieval
API with default advanced stages explicitly disabled (not pretending unsupported rerank works).

## Backend seams to deliver first

Concrete first UI/API contract: `GET /v1/knowledge-base-options` returns `{models, bindings,
tokenizers, supported_extensions, max_upload_bytes}`. Models expose public id/display_name/model_key/
dimension/max_input_tokens/tokenizer_id; bindings expose id/name/kind/driver only. Tokenizers is a
list of configured IDs. Management uses `/v1/model-providers` and `/v1/models` GET/POST, DELETE by ID
and `POST /v1/models/{id}/test`. Provider creation body: name,family,base_url,config (binding_revision,
allow_private,max_batch_size). Model creation matches RegisterModelSpec with prefixed provider_id.
`POST /v1/knowledge-bases/{kb}/documents/upload` accepts multipart field `file`, returns one
DocumentRegistrationResponse; `GET /.../documents/{doc}/download` is scoped authenticated download.

- Admin-only provider creation/list/removal with TEI/Infinity endpoint, trusted-private-network
  opt-in and binding revision; expose model registration/list with dimension, token limit, tokenizer
  ID, normalization/prefix. Reject unsupported invocation families in the first-product UI.
- Authenticated usable-model choices for KB creation; no credentials or private provider config in
  ordinary member responses. Admin storage create/test/list/delete reuses existing catalog endpoints;
  add safe binding choices for KB creation rather than granting platform:storage to content users.
- Admin may configure supported local storage under the deployment-provisioned root. Contributor
  uploads cannot choose filesystem paths or object keys. Default binding uses the shared persistent
  root; no UI promise of an unimplemented S3 driver.
- Actual document upload endpoint under a KB, authenticated `kb:write`, bounded bytes and filename,
  supported MIME/extension matching, streaming or bounded spool, SHA256 server calculation and
  canonical scoped object key. Register only after object write; handle duplicate content safely.
- Authenticated source download and nested read/delete/retry/chunk routes must bind the path KB to
  actual resource ownership, not only check same-workspace membership. No unauthenticated local
  presigned path exposure. Content is untrusted; render as text, never HTML.
- Dynamic registered provider settings must become available to query workers without manually
  copying IDs into environment variables. Publish a safe provider runtime projection from control
  to Redis and refresh it with maintenance; data-plane reads shared DTOs, not modelgw ORM. Secrets,
  if supported, are kept encrypted and never returned; otherwise clearly reject credential fields
  rather than accepting ignored credentials. Operator-configured tokenizer IDs/files remain an
  explicit immutable asset set shared by ingestion/retrieval processes; UI chooses available IDs.

## Compatibility and error contracts

Preserve current registered-upload endpoint for API clients; add actual byte upload separately.
Retain public prefixed IDs, RFC9457 errors, real permission dependencies and CSRF rules. Refresh
OpenAPI + generated TS schema after all routes land, never manually invent generated declarations.
Polling cancels on navigation; handle progress/error/loading/empty states. Destructive actions require
confirmation and display queued cleanup state until removed. Unsupported PDF/OCR is visibly disabled
and rejected by backend; no new parser acceptance claimed.

## Browser/HTTP acceptance

Fresh admin login/forced change -> configure model/provider and local/pgvector bindings -> create KB
-> upload Markdown and Office -> monitor indexed state -> inspect/edit child chunk -> query cited text
-> retry injected failure -> replace/delete source -> restart workers -> confirm data persists.
Also test member denied admin setup, cross-KB IDs, too-large/unsupported upload, duplicate upload,
HTTP errors, and no sensitive fields in model/storage selection responses. A rendered UI with mocked
requests is not enough: browser smoke must use the real HTTP stack and durable workers.
