"""A namespace whose payload indexes were never built gets them on next use.

If index creation failed after the collection was created, a retried index task
finds the collection present; without this check filters, counts and deletes
would scan every point from then on.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from cairn.vectorstore.base import Namespace, NamespaceSpec


async def test_missing_payload_indexes_are_created_for_an_existing_collection() -> None:
    url = os.environ.get("CAIRN_QDRANT__URL")
    if url is None:
        pytest.skip("needs CAIRN_QDRANT__URL")
    from qdrant_client import AsyncQdrantClient, models

    from cairn.vectorstore.qdrant import DENSE, PATHS, QdrantVectorStore

    client = AsyncQdrantClient(url=url, timeout=10)
    ns = Namespace(kb_id=uuid4(), index_version=1)
    await client.create_collection(
        ns.key(),
        vectors_config={DENSE: models.VectorParams(size=4, distance=models.Distance.COSINE)},
    )
    store = QdrantVectorStore(client)
    try:
        await store.ensure_namespace(ns, NamespaceSpec(dim=4, sparse=False))

        schema = (await client.get_collection(ns.key())).payload_schema
        assert set(schema) >= {"document_id", PATHS, "created_at"}
        assert schema["created_at"].data_type == models.PayloadSchemaType.INTEGER
    finally:
        await store.drop_namespace(ns)
        await store.close()
