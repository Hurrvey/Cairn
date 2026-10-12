# S3 storage, MCP document reading and Dify

September 29 implementation. This expands the existing knowledge product; it does not close
the annotated PDF quality gate, production sandbox review, or later pipeline/evaluation phases.

## S3-compatible document storage

In **Storage**, add a document binding and select **S3-compatible**. Enter an existing bucket,
region and optional key prefix. For MinIO/OSS/COS, supply the endpoint and explicit credentials.
For AWS, omit endpoint to use the SDK's normal AWS endpoint and, if no explicit key is entered,
the deployment's default credential chain. Private endpoints require **Allow this endpoint on
a private network**. Custom endpoints use path-style addressing.

Credentials are write-only and envelope-encrypted under `CAIRN_MASTER_KEY`, bound to their
workspace and purpose. The API returns `has_credentials`, never plaintext. The binding menu's
**Replace credentials** action also supports `PUT /v1/storage-bindings/{id}/credentials`.
Workers fetch the current sealed reference on their next task; the client registry compares
credential/configuration fingerprints. Existing streams finish on their leased old client,
which then closes. Fresh operations use the replacement. Keep the master key backed up.

Optional local service:

```text
# Set MINIO_ROOT_USER and a unique MINIO_ROOT_PASSWORD in .env first.
docker compose --profile s3 up -d --build minio
```

The console is loopback-only at `http://127.0.0.1:9001`; create a bucket there. Cairn containers
use endpoint `http://minio:9000` with private networking enabled. The optional image builds the
official fixed MinIO source revision in `deploy/minio/Dockerfile`; it does not depend on removed
public MinIO binary downloads. MinIO is a separate AGPLv3 service. Persistent `miniodata` is
retained when stopping Compose. This profile is optional; local object storage remains default.

Connection guards validate each actual socket IP, DNS result and custom-endpoint origin,
including redirects. SDK proxies are disabled; TLS certificate checks remain enabled.
Streaming writes buffer one 16 MiB multipart part, abort incomplete uploads on error, and
bounded reads recheck the byte cap while receiving data. Storage failures distinguish transient
network/5xx errors from invalid credentials/configuration.

## MCP directory and indexed passages

The existing Streamable HTTP `/mcp` now offers:

- `list_knowledge_bases`: up to 500 authorized directory entries and readiness; names/counts
  come from the Redis projection, with bounded refresh after indexing/deletion.
- `search_knowledge_base`: accepts the HTTP `filters` shape in addition to existing parameters.
- `get_document`: requires `kb:read`; returns ordered `passages`, document/index/revision facts
  and `truncated`. Supply `knowledge_base_id` when more than one KB is readable. Default text
  budget is 50,000 characters, maximum 200,000, additionally bounded by response bytes.

Document reading returns **indexed passages**, which may overlap; it never reconstructs the
original file or expands stale parent snapshots. Older indexes need rebuilding to contain
schema-2 document facts and ordinals. Scans cap at 10,000 points, 8 MiB selected payload and
10 seconds. Oversized resource envelopes return an explicit error. Tools/resources enforce
the same `kb:read` access; `kb:query` alone grants search, not document reading.

Resources: `cairn://kb/{kb_id}` and template
`cairn://kb/{knowledge_base_id}/documents/{document_id}`. Search citations supply document IDs.

## Dify External Knowledge

Configure Dify's external endpoint as the Cairn base URL (without `/retrieval`), the Cairn API
key, and the public `kb_...` identifier as External Knowledge ID. Dify appends `/retrieval`.
The key needs `kb:query` for that KB. An empty registration probe returns 400 for a valid key,
403 for an invalid key. The data/all role serves the adapter; control-only does not.

```json
{"knowledge_id":"kb_YOUR_ID","query":"How do we rotate keys?",
 "retrieval_setting":{"top_k":5,"score_threshold":0.5}}
```

Responses contain `records` with content/title/metadata and relevance in [0,1]. Native Cairn
ranking is retained, and `metadata.cairn_score` preserves its original score. Relevance uses
rerank scores when present, otherwise cosine similarity. Missing dense scores are computed by
one point-restricted query against the same frozen ACTIVE namespace. Parent hits inherit an
existing representative child's score; when missing, the highest matched-child cosine is used.
Dot/L2 indexes require normalized embeddings for this mapping, otherwise the request is refused.
Dify's threshold is applied afterward and does not replace the KB's native threshold.

Metadata supports equality, numeric ranges, in/not-in and existence. Substring operators are
explicitly rejected. Errors use `error_code`/`error_msg`; auth codes1001/1002 and missing-runtime
2001 follow Dify conventions. Rate limits retain Retry-After. Degradation is visible in the
`X-Cairn-Degraded` header and `metadata.cairn_degraded`.

Contract checked against [Dify's official specification](https://docs.dify.ai/en/cloud/use-dify/knowledge/external-knowledge-api)
and [registration implementation](https://github.com/langgenius/dify/blob/main/api/services/external_knowledge_service.py).
Automated HTTP/SDK tests are distinct from deployment against a live Dify installation.
