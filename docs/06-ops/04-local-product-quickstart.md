# Supported-format local product deployment

Status: supported local product accepted on2026-09-15; see
[`29-main-product-acceptance.md`](../04-plan/29-main-product-acceptance.md) for final evidence and limits.

## Prerequisites

- Docker Engine/Compose v2 with at least 4 CPU cores and 8 GiB memory available.
- `uv` and network access for the initial pinned dependencies/model download.
- Run commands from the repository root. PostgreSQL/Redis/object data use named Docker volumes;
  Qdrant vectors also use a named volume; model assets are mounted read-only. Do not run `down -v` on a deployment whose data you want to keep.

## First startup (Windows, Linux, macOS)

```text
uv run python scripts/init_env.py
uv run python scripts/prepare_embedding_model.py
docker compose --profile local-model up -d --build
docker compose logs migrate
```

The environment initializer refuses to overwrite an existing `.env`. It creates random secrets
without printing them. Preserve/back up the master key with your database. Existing installations
must retain their credentials and follow migration notes; never regenerate secrets to fix startup.
An installation created before the Qdrant switch (2026-09-24) needs one new line in `.env`:
`QDRANT_API_KEY=<a long random value>`; its vector indexes are rebuilt from documents after upgrade.
Keep PostgreSQL on the `16-bookworm` image the Compose file pins: it has the same glibc as the old
pgvector image, so existing text indexes keep their order. If a server logs a collation version
mismatch, run `REINDEX DATABASE` followed by `ALTER DATABASE ... REFRESH COLLATION VERSION`.
The model preparer pins MiniLM revision1110a243 and verifies SHA256 of every downloaded file.

Open `http://127.0.0.1:8080`, sign in using the initial password from migration logs, and complete
the required password change. The default listens only on loopback and uses local HTTP cookies.
Remote deployments require HTTPS, secure cookies, proper network controls and a backup plan.

## Configure through the UI

1. **Models → Add provider**: name `Local MiniLM`, TEI endpoint `http://embedding:80`, enable trusted
   private networking, immutable deployment revision `minilm-1110a243`, batch limit16.
2. **Register model**: `sentence-transformers/all-MiniLM-L6-v2`, dimension384, input token limit256,
   tokenizer `minilm`, normalization enabled. Run **Test** to verify actual model execution.
3. **Storage**: create a local object binding and a Qdrant vector binding (the connection comes from
   `CAIRN_QDRANT__URL`; the binding has no settings). Local files use the shared
   `/var/lib/cairn/objects` volume; no per-container ephemeral path is used.
4. **Knowledge bases**: create one with the model and both bindings, upload supported documents,
   watch each document move through the pipeline, open it to inspect/edit chunks, and query in the
   **Search** section. The overview page lists these setup steps until they are complete.

Supported uploads: TXT, Markdown, HTML, JSON, DOCX, PPTX, XLSX, CSV, PDF, PNG, JPEG and single-page
TIFF, subject to parser safety and size limits. Scanned pages use bounded local Tesseract OCR
with English/simplified/traditional Chinese packs. External TEI/Infinity may replace the optional local
model; deploy its matching tokenizer to all workers/data processes and choose that configured ID.
Do not pretend that changing a tokenizer file in place preserves old index provenance.

## Processes and lifecycle

Compose starts API/web plus parse, chunk, embed, index and maintain workers. The maintain worker
automatically repairs runtime projections after cache loss and renews them before TTL expiry.
Deletion tasks remove scoped vectors/files before final database cleanup. Old retired namespaces
are dropped only after retention and never when active/building. Monitoring must include worker
logs and task failures; dead-lettered work must not be mistaken for completed cleanup.

```text
docker compose ps
docker compose logs --tail 100 worker-maintain worker-parse worker-chunk worker-embed worker-index
docker compose restart worker-parse worker-chunk worker-embed worker-index worker-maintain
docker compose stop
docker compose --profile local-model up -d
```

The web proxy sends API-key retrieval to the data-only API and authenticated browser retrieval to
the all-role control instance, preserving session/CSRF handling. Nothing marks retrieval as public.
Provider projection and upload APIs are part of the supported flow; no manual ORM seeding is needed.

## Acceptance and remaining limits

### Oversized chunks on older builds

The September16 long-document regression exposed default512-token chunks with a256-token MiniLM
model. The repair is tracked in document30. Do not increase the registered model limit beyond its
real capacity or truncate source content. Corrected workers enforce the version-owned model budget;
retry of the specific old `EMBED_INPUT_TOO_LARGE` embed failure reuses parsed content but rebuilds
the oversized chunk artifact. Automatic boundary repair is refused for custom output or documents
with manual chunk edits, rather than silently moving those edits to different source positions.

After installing the corrected image, use the document's **Retry** action. A retry on the old image
only repeats embedding on the same oversized chunks and cannot resolve this configuration mismatch.

This preset does not include S3 or arbitrary custom functions. PDF/OCR is a bounded baseline;
complex tables and the original annotated-corpus quality gate remain open. Reranking requires
an explicitly configured provider; the bundled MiniLM embedding model is not a reranker.
The read-only MCP search increment is described in [MCP quickstart](05-mcp-quickstart.md);
its broader document/resource browsing and Agent-side integration are not included.
PDF/OCR draft collection errors have been replaced by executable parser tests. A runnable
journey is not a claim that all original document-quality milestones are complete. See
[PDF quality evaluation](../05-quality/04-pdf-ocr-evaluation.md). Run the documented product smoke only against disposable environments: it creates
models, storage, KBs and uploads, and uses passwords from environment variables rather than logs.
