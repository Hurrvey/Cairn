# Archive import — reviewed design

Status: approved direction by persistent `/root/design_advisor`; queued after quota admission.
Coordinator plans, fixed `/root/developer` implements. This is an import job, not a parser that
pretends many independent files are one ParsedDocument.

## User flow and API

Upload ZIP to `POST /v1/knowledge-bases/{kb_id}/archive-imports` (multipart ZIP plus optional
default document metadata), kb:write required,202 import identity/status. List/detail/entry list
and JSONL manifest export require kb:read; write implies read. Nested ownership and workspace
checks apply everywhere. Cancel is idempotent; retry restarts only pending/retryable failed
entries while source is retained, never successful receipts or permanent safety failures.

Routes under the import ID: GET detail, GET entries with pagination/status filter, POST cancel,
POST retry, GET manifest. No object keys, credentials or original ZIP bytes in manifest output.
UI shows validated/importing/partial/cancelled states, accepted/deduplicated/unsupported/failed
counts and per-member results. Cancellation preserves children already committed; clearly state
this before cancellation rather than pretending the import is a rollback transaction.

## Durable manifest and source ownership

Catalog owns archive_import and archive_import_entry rows. Import tracks workspace/KB/creator,
source key/hash/size, policy version, state, task/import generation, cancellation, timings and
source retention. Entries keyed by import ID/index store original and normalized name, type,
declared/actual size,CRC,content hash,MIME,status,document ID and stable result/error code.
Mutable manifest authority is PostgreSQL; JSONL export is a bounded derived view.

ZIP key is `{workspace}/{kb}/imports/{import_id}/source.zip`, unique per import. First create
staging identity, write source, then atomically transition/enqueue validation. Abandoned staging
is maintenance-cleaned by age/state/generation, never inferred executable merely from object
existence. Source retention24h after terminal status; manifest retention30d. KB prefix purge
owns source cleanup. Temporary files are bounded and removed on every exit path.

ZIP staging does not consume registered-document bytes quota, but has its own workspace limits
for concurrent imports and total staged bytes. Source max50MiB; enforce request/body limits in
middleware and nginx as well as handler. Existing doc quota applies independently to each child.

## Two-phase safety validation

Complete archive safety/CRC preflight before registering any children, so an unsafe final member
cannot leave earlier documents imported. First inspect directory and actually stream-decompress
all entries under budgets; discard data or bounded-spool. Second pass imports validated entries.

Initial policy: ZIP_STORED/DEFLATED only; encrypted archives, symlinks/special files and nested
generic archives rejected. DOCX/PPTX/XLSX/EPUB ZIP packaging is allowed through existing MIME/
container validation, not rejected merely for ZIP magic. Unsupported regular file formats receive
per-entry unsupported results after safety validation, without failing safe siblings.

Member paths never become filesystem paths. Reject absolute/UNC/drive paths,backslash,NUL,..,
duplicate normalized paths and excessive lengths/depth; use UUID/index for temporary filenames.
Max10,000 entries including directories,2GiB total expansion,10x compression ratio,50MiB each
document; enforce actual bytes as well as declared sizes and a killable wall-time budget. With
50MiB source and10x expansion, ratio often limits before2GiB, intentionally. CRC/truncation fails
whole import before children. Malformed central-directory discrepancies do not bypass budgets.

## Atomic child receipt seam

Add a catalog-owned narrow facade that verifies task lease/import generation/entry state and KB
liveness, reuses canonical object+document+run+parse task+quota registration, and commits the entry
receipt in the same SQL transaction. Avoid a worker loop that commits upload then manifest in a
second transaction. No arbitrary post-commit callback to worker code and no second duplicated
quota/register implementation.

Receipt is terminal for successful entry even if its child document is later deleted by user;
retry must not resurrect it. Same bytes under different member names may dedup to one existing
document; do not overwrite that document's title/metadata/source. The manifest retains each source
member independently. Task/import generation and entry locks prevent stale workers committing.

Add Document.source_type=`archive`, migration and DTO/UI filters; source_ref can name stable
archive/member identity. Receipt/lineage is system-owned, not user-overridable metadata.

## Retry, cancellation, idempotency and deletion

Same Idempotency-Key+source hash+parameters returns same import within a bounded window; conflicting
reuse is409. Without key, identical ZIP starts a new import and child hash dedup applies. Do not
permanently memoize ZIP hash and prevent intentional reimport.

Cancel prevents subsequent entry commits once confirmed but does not delete successful children;
one concurrent entry may commit before confirmation. Worker checks cancellation/generation at
each commit. Temporary transient errors retry boundedly; quota failure yields partial with explicit
retry after limits increase until later automatic blocked/resume support exists. Source expiry
makes retry unavailable with a clear reason.

KB deletion cancels/drains import work before removing source prefix; no child can register into
a deleting KB. Child quota release remains catalog purge responsibility. Maintenance source and
manifest pruning cannot erase an active import's source or delete another import's independent key.

## Acceptance

Real PG/task/object-store tests: unsafe last entry creates zero children; ZIP-wrapped Office files
work; hash dedup/idempotency; crash after child commit resumes receipt; deleted child never revived;
quota-mid-import partial; cancel preserves receipts; competing worker fences; source-retention/KB
purge; unsupported entries/report pagination. Security tests cover zip slip,Unicode/path collisions,
symlink/encryption,declared vs actual size,bombs,CRC and truncated archives. Browser upload/report/
retry/cancel uses actual pipeline. Independent coordinator and reviewer acceptance precedes release.
