# Cairn TypeScript client

Node.js 20+ and modern browsers. Point the client at Cairn's unified nginx URL. The API key is
stored in a private class field, requests omit ambient browser credentials and referrers, and
redirects are never followed.

```ts
import { CairnClient } from "@cairn/client";

const client = new CairnClient("https://knowledge.example.com", process.env.CAIRN_API_KEY!);
const result = await client.retrieve({
  targets: [{ knowledge_base_id: "kb_YOUR_ID" }],
  query: "How do I rotate a key?",
});
console.log(result.results, result.degraded, result.partial_failures);
```

The default is zero retries. Opt-in retries apply only to GET/HEAD and retrieval query/batch;
mutations and successful batch responses with partial failures are never replayed. One combined
AbortSignal enforces the overall timeout across fetch and bounded response reads. Caller
cancellation is accepted by every typed method, including retrieval, batch, CRUD, uploads,
document-content reads, chunk operations and paginated iteration.
Document uploads accept a `Blob`; set its MIME type to a server-supported type, for example
`new Blob([markdown], { type: "text/markdown" })`.

Run `npm run generate` after changing `apps/web/openapi.json`, then `npm test`, `npm run typecheck`
and `npm run build`. `npm pack` builds a local tarball; this package does not publish itself.
