# Cairn Python client

Python 3.11+, sync and async clients. Point `base_url` at Cairn's unified nginx URL;
discovery and citation reads are not available on a bare data-only API instance.

```python
import os
from cairn_client import CairnClient, RetrievalRequest

with CairnClient("https://knowledge.example.com", os.environ["CAIRN_API_KEY"]) as client:
    answer = client.retrieve(RetrievalRequest(
        targets=[{"knowledge_base_id": "kb_YOUR_ID"}], query="How do I rotate a key?"
    ))
    print(answer.results, answer.degraded, answer.partial_failures)
```

Use `AsyncCairnClient` with `async with`/`await` for asynchronous applications. Models are
generated from the server's OpenAPI contract. Defaults: no automatic retry, no redirects,
no environment proxies, bounded response reads. Opt-in retries apply only to declared read
operations; mutations and successful batch partial failures are never replayed. API-key
permissions remain server-enforced. Keys never appear in client repr or SDK errors.

`AsyncCairnClient` enforces one hard deadline across send, retries, waits, and response reads.
The synchronous client enforces a soft aggregate deadline plus an HTTPX timeout on each network
operation. A real blocking read may therefore finish as late as one I/O timeout after the aggregate
deadline; after that read returns the client closes the response and starts no further retry.

Run `./generate_models.ps1` after changing `apps/web/openapi.json`; it pins the generator and
formats the generated file deterministically. Build locally with `uv build` from this directory.
No registry publishing is performed automatically. Optional LangChain and LlamaIndex adapters are
implemented against LangChain Core 1.6 and LlamaIndex Core 0.14 APIs. They preserve native Cairn
scores, citation IDs and degradation metadata; they do not read source URLs or repeat mutations.

```python
from cairn_client.adapters import CairnLangChainRetriever

with CairnClient("https://knowledge.example.com", os.environ["CAIRN_API_KEY"]) as client:
    retriever = CairnLangChainRetriever(client=client, knowledge_base_ids=["kb_YOUR_ID"])
    documents = retriever.invoke("How do I rotate a key?")
```
