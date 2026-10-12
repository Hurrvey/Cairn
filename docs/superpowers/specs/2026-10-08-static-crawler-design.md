# Static crawler — reviewed implementation direction

Status: approved direction from persistent design advisor, queued after quota/archive slices.
Root plans and accepts; the fixed developer implements. This slice is not JavaScript rendering.

## Durable state and product flow

Job owns KB/owner/enabled/config version/seeds/scope/UTC cron/next due/removal policy. Run freezes
configuration and generation, tracks manual/scheduled trigger, deadlines/budgets and outcome.
URL state is unique by normalized URL within job and preserves ETag/Last-Modified/body hash,
document relation, confirmed status and outlinks. Separate run-URL frontier holds depth, scheduling,
claim fence and result receipt, so current work does not overwrite historical run truth.

Expose job CRUD/manual run/cancel, paginated runs and per-URL reports. kb:write for changes/run,
kb:read for reports; nested workspace/KB ownership always checked. Scheduler validates5-field
UTC cron, atomically creates run/enqueues/advances next_due, and uses a DB uniqueness rule for
one active run per job. Missed intervals coalesce to one run rather than a replay storm.

## Atomic source receipt and document ownership

Existing document dedup `(kb_id,content_hash)` means URLs can share documents with other URLs or
user uploads. Persist URL→document relation and job ownership separately from user metadata.
Never unconditionally revise or delete the shared document.

- A job-created, currently exclusive and unmodified document may get a new revision.
- Shared/non-crawl-owned content changes register/reuse another document and rebind this URL.
- Hash collision with existing content only rebinds; it does not overwrite title/metadata/source.
- Auto-delete requires job ownership, no other live URL reference and no manual modification.

A narrow catalog facade commits URL receipt+document/revision+quota+parse-task together with
task/import generation fencing. Do not call standalone start_revision then commit URL separately.
Replayed response cannot create another revision, resurrect deleted KB or reclaim a user document.

## Network scope and extraction

Default allowlist is exact seed origin (scheme/host/effective port), not domain suffix matching.
Use SafeTransport with private-address access explicitly false and environment proxies disabled.
Manually handle at most5 redirects: validate origin/path/scheme before each hop, prohibit HTTPS
downgrade, then let SafeTransport validate/pin DNS. Caller cookies/keys never go outbound.

Normalize host/default port and remove fragment, but preserve query parameter meaning/order.
Reject userinfo,non-HTTP(S),oversized URLs. Resolve relative links against final response URL;
HTML base href does not expand scope. Extract frontier links from bounded original HTML separately
from HtmlParser's main-content extraction (which removes links).

First slice accepts bounded HTML/XHTML only and rejects obvious binary despite claimed MIME.
PDF/Office/archive/image links are reported unsupported. Page bytes are capped after decompression;
all network requests count toward run bytes/deadline, including robots and sitemaps.

## Robots, sitemaps and safe scope rules

Follow robots by default. Only a workspace administrator can configure an override. Cache parsed
rules with explicit1h expiry and byte bounds. Robots network/5xx failure blocks crawling for that
origin;404 means no rules. Cross-origin robots redirects are rejected as a documented stricter
policy than the full RFC possibilities; never interpret parse exceptions as allow-all.

Sitemap XML disables DTD/external entities. Bound XML/gzip expansion,recursive index depth,total
fetches and discovered URLs. Every sitemap URL still passes scope/robots/frontier dedup.

First slice supports explicit path-prefix and finite glob scope, not arbitrary Python regex.
Unsupported regex constructs are rejected at configuration; a thread around unbounded regex is
not a CPU safety boundary. Record this deviation from the original regex wording in module/API
docs. `render_js=true` is explicitly rejected until browser-wide egress protection ships.

## Cross-worker politeness and recovery

Per-origin shared permits enforce start delay,concurrency and Retry-After across jobs/workers.
Permits have bounded owner TTL/fence; request deadline is shorter than permit TTL; cancellation
releases safely. Missing permit store pauses/defers fetching rather than uncontrolled fail-open.
The fetch queue's process concurrency is not a website-specific politeness guarantee.

Run bounds include depth,max pages,frontier count,single/total bytes and total deadline. Reaching
any cap produces partial/limit_reached. Cancel prevents new scheduling and advances generation
so late responses cannot commit. Already registered documents remain visible in partial report.
KB deletion cancels/drains KB fetch work; source confirmation checks liveness again.

304 requires a reusable confirmed body/document receipt, otherwise perform one unconditional GET
or report protocol error. Reuse stored outlinks on304 so unchanged roots do not hide descendants.
200 unchanged content avoids revision/parse/embed, but refreshes outlinks and fetch facts.

## Removal semantics

Default remove_missing=false. When enabled, only a known URL's successfully completed GET with
confirmed404/410 may cause deletion, subject to ownership/reference/manual-edit checks. Not-seen,
robots denial,401/403,429,5xx,timeouts,scope edits,depth/page caps and cancellation never imply gone.
Persist requested/final alias information so redirect source/target do not delete each other's
shared documents. Job deletion disables schedules and cancels work; content retention/deletion
is a separate explicit policy, not an implicit destructive side effect.

## Acceptance

Real task/PG/object-store tests verify restart frontier recovery,one receipt per URL,generation
fences,304 graph traversal,unchanged no-reembedding,shared-document updates/deletion protections,
quota-partial recovery,cron overlap and cancellation. Network tests cover private/metadata/mapped
IPv6/DNS changes,redirect scope,hostile base tags,robots unavailable,sitemap bombs/loops and
cross-worker delay/permits. Browser completes job→run→incremental recrawl→cancel→per-URL report.
No live public-site load tests beyond explicit small read-only evidence; use controlled fixture
sites for failure/scope/robots and deletion scenarios.

Reviewed deviations: static-only; prefix/glob rather than arbitrary regex; exact-origin robots
redirect policy; HTML-only sources;404/410+ownership removal instead of absence inference.
JavaScript rendering later requires subresource/redirect/WebSocket/service-worker egress control,
not merely SafeTransport for the seed URL.
