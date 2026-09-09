"""Catalog — TC-M03-01 … TC-M03-17.

The properties that matter here are the ones whose failure mode is *silent*:

* embedding immutability (ADR-0006), which if it leaked would produce a mixed
  embedding space that returns plausible-looking but wrong results;
* blue/green index versioning (ADR-0007), where a premature switch would serve
  queries from a half-built index;
* runtime publication (ADR-0002), where a stale projection would keep the data
  plane answering from retired configuration.

Everything else in the module is CRUD, and is covered to the extent that it
guards those three.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from cairn.authz.model import Principal
from cairn.authz.service import AuthzService
from cairn.catalog.config import ChunkConfig, RetrievalConfig
from cairn.catalog.dto import (
    ChunkSpec,
    CreateKbSpec,
    ReindexEstimate,
    ReindexSpec,
    UpdateKbSpec,
    UploadSpec,
)
from cairn.catalog.errors import (
    BindingInUse,
    ChunkNotEditable,
    EmbeddingModelImmutable,
    KbIndexInProgress,
)
from cairn.catalog.service import CatalogService, slugify
from cairn.core.db import session_scope, transaction
from cairn.core.errors import Conflict, NotFound
from cairn.identity.service import IdentityService
from cairn.modelgw.catalog import ModelCatalog
from cairn.modelgw.dto import RegisterModelSpec

STRONG = "Correct-Horse-Battery-9"
DIM = 8


@pytest.fixture
async def admin(identity: IdentityService, monkeypatch: pytest.MonkeyPatch) -> Principal:
    monkeypatch.delenv("CAIRN_INITIAL_ADMIN_PASSWORD", raising=False)
    result = await identity.bootstrap()
    assert result is not None and result.password is not None
    login = await identity.login("admin", result.password)
    assert login.change_token is not None
    await identity.complete_initial_setup(
        login.change_token, current_password=result.password, new_password=STRONG
    )
    principal = await AuthzService().principal_for_user(result.user_id)
    assert principal is not None
    return principal


@pytest.fixture
def models() -> ModelCatalog:
    return ModelCatalog()


@pytest.fixture
def catalog() -> CatalogService:
    return CatalogService()


@pytest.fixture
async def embedding_model(admin: Principal, models: ModelCatalog):  # type: ignore[no-untyped-def]
    provider = await models.create_provider(
        admin.workspace_id, name="local-tei", family="tei", base_url="http://tei:80"
    )
    return await models.register_model(
        admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id,
            model_key="bge-small",
            display_name="BGE Small",
            capability="embedding",
            dimension=DIM,
        ),
    )


@pytest.fixture
async def chat_model(admin: Principal, models: ModelCatalog):  # type: ignore[no-untyped-def]
    provider = await models.create_provider(
        admin.workspace_id, name="oai", family="openai", base_url="https://api.openai.com/v1"
    )
    return await models.register_model(
        admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id,
            model_key="gpt-4o",
            display_name="GPT-4o",
            capability="chat",
        ),
    )


@pytest.fixture(autouse=True)
def _reset_store_registries() -> None:
    from cairn.objectstore.registry import reset_object_registry
    from cairn.vectorstore.registry import reset_vector_registry

    reset_object_registry()
    reset_vector_registry()


@pytest.fixture
async def bindings(admin: Principal, catalog: CatalogService, tmp_path: Path):  # type: ignore[no-untyped-def]
    vector = await catalog.create_binding(
        admin, kind="vector", driver="pgvector", name="primary", config={}
    )
    objects = await catalog.create_binding(
        admin, kind="object", driver="local", name="primary", config={"path": str(tmp_path)}
    )
    return vector, objects


@pytest.fixture
async def kb(admin: Principal, catalog: CatalogService, bindings, embedding_model):  # type: ignore[no-untyped-def]
    vector, objects = bindings
    return await catalog.create_kb(
        admin,
        CreateKbSpec(
            name="Product Handbook",
            embedding_model_id=embedding_model.id,
            vector_binding_id=vector.id,
            object_binding_id=objects.id,
        ),
    )


async def _upload(catalog: CatalogService, admin: Principal, kb_id, name="a.pdf", digest="a"):  # type: ignore[no-untyped-def]
    return await catalog.register_upload(
        admin,
        kb_id,
        UploadSpec(
            filename=name,
            content_hash=(digest * 64)[:64],
            size_bytes=1024,
            mime_type="application/pdf",
            object_key=f"{admin.workspace_id}/{kb_id}/originals/{(digest * 64)[:64]}",
        ),
    )


# --- TC-M03-01 … 04: creation and configuration ------------------------------


async def test_create_kb_copies_dimension_from_the_model(kb, embedding_model) -> None:
    # Never taken from the request: a client-supplied dimension that disagreed
    # with the model would corrupt every vector written.
    assert kb.embedding_dim == DIM
    assert kb.embedding_model_id == embedding_model.id
    assert kb.active_index_version is None
    assert kb.slug == "product-handbook"


async def test_create_kb_applies_documented_defaults(kb) -> None:
    assert kb.chunk_config == ChunkConfig()
    assert kb.retrieval_config == RetrievalConfig()
    assert kb.chunk_config.strategy == "parent_child"
    assert kb.retrieval_config.search_mode == "hybrid"


async def test_create_kb_rejects_a_chat_model(
    admin: Principal, catalog: CatalogService, bindings, chat_model
) -> None:
    """A KB built on a chat model fails at its first upsert — long after the
    documents are uploaded. It has to fail here instead."""
    vector, objects = bindings
    with pytest.raises(Exception) as exc:
        await catalog.create_kb(
            admin,
            CreateKbSpec(
                name="Wrong",
                embedding_model_id=chat_model.id,
                vector_binding_id=vector.id,
                object_binding_id=objects.id,
            ),
        )
    assert "embedding model" in str(exc.value).lower()


async def test_create_kb_rejects_a_swapped_binding_pair(
    admin: Principal, catalog: CatalogService, bindings, embedding_model
) -> None:
    vector, objects = bindings
    with pytest.raises(Exception) as exc:
        await catalog.create_kb(
            admin,
            CreateKbSpec(
                name="Swapped",
                embedding_model_id=embedding_model.id,
                vector_binding_id=objects.id,  # object binding in the vector slot
                object_binding_id=vector.id,
            ),
        )
    assert "vector binding" in str(exc.value).lower()


async def test_duplicate_slug_is_rejected(
    admin: Principal, catalog: CatalogService, bindings, embedding_model, kb
) -> None:
    vector, objects = bindings
    with pytest.raises(Conflict):
        await catalog.create_kb(
            admin,
            CreateKbSpec(
                name="Product Handbook",
                embedding_model_id=embedding_model.id,
                vector_binding_id=vector.id,
                object_binding_id=objects.id,
            ),
        )


def test_slugify_never_returns_an_empty_slug() -> None:
    assert slugify("产品手册") == "knowledge-base"
    assert slugify("  Hello, World!  ") == "hello-world"


# --- TC-M03-05 … 08: embedding immutability (ADR-0006) -----------------------


async def test_embedding_model_is_mutable_before_the_first_index(
    admin: Principal, catalog: CatalogService, kb, models: ModelCatalog
) -> None:
    """Nothing has been embedded yet, so there is no space to be inconsistent
    with — refusing here would be needless friction."""
    provider = await models.create_provider(admin.workspace_id, name="alt", family="tei")
    other = await models.register_model(
        admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id,
            model_key="e5-base",
            display_name="E5 Base",
            capability="embedding",
            dimension=DIM,
        ),
    )
    updated = await catalog.update_kb(admin, kb.id, UpdateKbSpec(embedding_model_id=other.id))
    assert updated.embedding_model_id == other.id


async def test_embedding_model_is_immutable_once_indexed(
    admin: Principal, catalog: CatalogService, kb, models: ModelCatalog
) -> None:
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)

    provider = await models.create_provider(admin.workspace_id, name="alt", family="tei")
    other = await models.register_model(
        admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id,
            model_key="e5-base",
            display_name="E5 Base",
            capability="embedding",
            dimension=DIM,
        ),
    )

    with pytest.raises(EmbeddingModelImmutable) as exc:
        await catalog.update_kb(admin, kb.id, UpdateKbSpec(embedding_model_id=other.id))
    # The message must name the way forward, not just the refusal.
    assert "reindex" in str(exc.value).lower()


async def test_metric_is_immutable_once_indexed(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)
    with pytest.raises(EmbeddingModelImmutable):
        await catalog.update_kb(admin, kb.id, UpdateKbSpec(metric="l2"))


async def test_database_trigger_refuses_a_direct_embedding_change(kb, admin, catalog) -> None:
    """Layer 3 of 3 (ADR-0006).

    The API and the service both reject this first, so this test bypasses them
    entirely — a raw UPDATE is what a data-fix script or a future module that
    has not read the ADR would issue, and the invariant has to hold for those
    too.
    """
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)

    with pytest.raises(Exception) as exc:
        async with transaction() as session:
            await session.execute(
                text("UPDATE knowledge_base SET embedding_dim = 1536 WHERE id = :id"),
                {"id": kb.id},
            )
    assert "immutable" in str(exc.value).lower()


async def test_trigger_permits_the_change_when_a_rebuild_opens(
    admin: Principal, catalog: CatalogService, kb, models: ModelCatalog
) -> None:
    """A reindex is the sanctioned path, and it must not be blocked by the
    backstop that exists to protect it."""
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)

    provider = await models.create_provider(admin.workspace_id, name="alt", family="tei")
    other = await models.register_model(
        admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id,
            model_key="e5-large",
            display_name="E5 Large",
            capability="embedding",
            dimension=16,
        ),
    )

    result = await catalog.start_reindex(
        admin, kb.id, ReindexSpec(embedding_model_id=other.id, confirm=True)
    )
    assert not isinstance(result, ReindexEstimate)
    refreshed = await catalog.get_kb(kb.id)
    assert refreshed.embedding_dim == 16
    assert refreshed.building_index_version == 2
    # The live version keeps serving while the new one builds.
    assert refreshed.active_index_version == 1


# --- TC-M03-09 … 12: blue/green index versions (ADR-0007) --------------------


async def test_reindex_without_confirm_estimates_and_starts_nothing(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    result = await catalog.start_reindex(admin, kb.id, ReindexSpec())
    assert isinstance(result, ReindexEstimate)
    assert (await catalog.get_kb(kb.id)).building_index_version is None


async def test_only_one_build_may_be_in_flight(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    """Two concurrent rebuilds would race to flip `active_index_version`, and
    the loser's namespace would leak."""
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    with pytest.raises(KbIndexInProgress):
        await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))


async def test_activate_switches_atomically(admin: Principal, catalog: CatalogService, kb) -> None:
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)

    view = await catalog.get_kb(kb.id)
    assert view.active_index_version == 1
    assert view.building_index_version is None
    assert view.status == "active"
    assert view.last_indexed_at is not None


async def test_activating_a_version_that_is_not_building_is_refused(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    with pytest.raises(Conflict):
        await catalog.activate_index_version(kb.id, 7)


async def test_a_failed_build_leaves_the_active_version_serving(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.fail_index_version(kb.id, 2, "embedding provider timed out")

    view = await catalog.get_kb(kb.id)
    assert view.active_index_version == 1
    assert view.building_index_version is None
    assert view.status == "active"

    # The error survives on the version row, so the failure stays diagnosable.
    versions = await catalog.list_index_versions(kb.id)
    failed = next(row for row in versions if row.version == 2)
    assert failed.state == "failed"
    assert "timed out" in (failed.error or "")


async def test_index_progress_reports_the_building_version(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    progress = await catalog.get_index_progress(kb.id)
    assert progress.building_index_version == 1
    assert progress.state == "building"


# --- TC-M03-13: runtime publication (ADR-0002) -------------------------------


async def test_runtime_is_published_only_once_a_version_is_active(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    # Nothing indexed yet, so there is nothing the data plane could serve.
    assert await catalog.publish_runtime(kb.id) is None

    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)

    runtime = await catalog.publish_runtime(kb.id)
    assert runtime is not None
    # ACTIVE, never "latest" — this is what stops a query reaching a half-built
    # index during a rebuild.
    assert runtime.index_version == 1
    assert runtime.embedding_model.dimension == DIM
    assert runtime.vector_binding.driver == "pgvector"


async def test_config_change_bumps_the_version_so_cached_entries_go_stale(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    before = kb.config_version
    await catalog.update_kb(admin, kb.id, UpdateKbSpec(name="Renamed"))
    assert (await catalog.get_kb(kb.id)).config_version > before


async def test_chunk_config_change_flags_a_required_rebuild(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)

    updated = await catalog.update_kb(
        admin, kb.id, UpdateKbSpec(chunk_config=ChunkConfig(child_tokens=256))
    )
    # Surfaced so the UI can prompt, rather than leaving the user wondering why
    # their new chunk size changed nothing.
    assert updated.reindex_required is True


# --- TC-M03-14 … 15: documents ------------------------------------------------


async def test_register_upload_creates_the_document_and_its_task_together(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    """NFR-R-03. If these could commit separately, a crash between them leaves a
    document nothing will ever process."""
    result = await _upload(catalog, admin, kb.id)
    assert result.status == "accepted"
    assert result.document is not None
    assert result.document.state == "registered"

    async with session_scope() as session:
        queued = await session.scalar(
            text("SELECT count(*) FROM task WHERE document_id = :id"),
            {"id": result.document.id},
        )
    assert queued == 1


async def test_identical_content_is_skipped_not_rejected(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    """Re-uploading identical content is the expected outcome of a retried sync.
    Reporting it as an error trains users to ignore errors."""
    first = await _upload(catalog, admin, kb.id)
    second = await _upload(catalog, admin, kb.id, name="a-copy.pdf")

    assert second.status == "skipped"
    assert second.reason == "DUPLICATE_CONTENT_HASH"
    assert first.document is not None
    assert second.existing_document_id == first.document.id


async def test_documents_are_deduped_per_knowledge_base_not_globally(
    admin: Principal, catalog: CatalogService, bindings, embedding_model, kb
) -> None:
    vector, objects = bindings
    other = await catalog.create_kb(
        admin,
        CreateKbSpec(
            name="Support Runbook",
            embedding_model_id=embedding_model.id,
            vector_binding_id=vector.id,
            object_binding_id=objects.id,
        ),
    )
    await _upload(catalog, admin, kb.id)
    assert (await _upload(catalog, admin, other.id)).status == "accepted"


async def test_document_state_counts_drive_the_progress_view(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    await _upload(catalog, admin, kb.id, name="a.pdf", digest="a")
    await _upload(catalog, admin, kb.id, name="b.pdf", digest="b")
    assert (await catalog.document_state_counts(kb.id))["registered"] == 2


async def test_upload_is_refused_once_a_kb_is_deleted(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    """`delete_kb` soft-deletes, and every catalog read excludes soft-deleted
    rows — so a late upload gets a 404 rather than being accepted into a
    knowledge base that is midway through being purged."""
    await catalog.delete_kb(admin, kb.id)
    with pytest.raises(NotFound):
        await _upload(catalog, admin, kb.id)


# --- TC-M03-16 … 17: chunks ---------------------------------------------------


async def _chunked(catalog: CatalogService, admin: Principal, kb_id, count=3):  # type: ignore[no-untyped-def]
    registration = await _upload(catalog, admin, kb_id)
    assert registration.document is not None
    doc_id = registration.document.id
    await catalog.replace_chunks(
        kb_id,
        doc_id,
        1,
        [
            ChunkSpec(
                id=uuid4(),
                document_id=doc_id,
                ordinal=i,
                content=f"chunk {i}",
                content_hash=f"{i:064d}",
                token_count=4,
            )
            for i in range(count)
        ],
    )
    return doc_id


async def test_edited_chunks_survive_a_reindex(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    """FR-F-09. A manual fix to a badly-parsed table must survive the next
    rebuild, or the user loses the work every time chunking is retuned."""
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)
    doc_id = await _chunked(catalog, admin, kb.id)

    chunks = await catalog.list_chunks(kb.id, doc_id, index_version=1)
    await catalog.edit_chunk(admin, kb.id, chunks[1].id, "corrected table")

    # Re-chunking the same document, as a rebuild would.
    await catalog.replace_chunks(
        kb.id,
        doc_id,
        1,
        [
            ChunkSpec(
                id=uuid4(),
                document_id=doc_id,
                ordinal=i,
                content=f"freshly parsed {i}",
                content_hash=f"{i:064d}",
                token_count=4,
            )
            for i in range(3)
        ],
    )

    after = await catalog.list_chunks(kb.id, doc_id, index_version=1)
    assert after[1].content == "corrected table"
    assert after[1].is_edited is True
    # The untouched ones are replaced as normal.
    assert after[0].content == "freshly parsed 0"


async def test_a_chunk_outside_the_active_version_is_not_editable(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    """An edit to a building version would be discarded by the next switch,
    which looks like data loss."""
    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 1)
    doc_id = await _chunked(catalog, admin, kb.id)
    chunks = await catalog.list_chunks(kb.id, doc_id, index_version=1)

    await catalog.start_reindex(admin, kb.id, ReindexSpec(confirm=True))
    await catalog.activate_index_version(kb.id, 2)

    with pytest.raises(ChunkNotEditable):
        await catalog.edit_chunk(admin, kb.id, chunks[0].id, "too late")


# --- storage bindings ---------------------------------------------------------


async def test_binding_in_use_cannot_be_deleted(
    admin: Principal, catalog: CatalogService, bindings, kb
) -> None:
    vector, _ = bindings
    with pytest.raises(BindingInUse):
        await catalog.delete_binding(admin, vector.id)


async def test_unused_binding_can_be_deleted(
    admin: Principal, catalog: CatalogService, tmp_path: Path
) -> None:
    spare = await catalog.create_binding(
        admin, kind="object", driver="local", name="spare", config={"path": str(tmp_path / "s")}
    )
    await catalog.delete_binding(admin, spare.id)
    assert [b.id for b in await catalog.list_bindings(admin.workspace_id)] == []


async def test_binding_health_is_verified_at_creation(
    admin: Principal, catalog: CatalogService, bindings
) -> None:
    """A misconfigured backend must fail at configuration time, not when the
    first document is uploaded."""
    vector, objects = bindings
    refs = {b.id: b for b in await catalog.list_bindings(admin.workspace_id)}
    assert refs[vector.id].health_state == "healthy"
    assert refs[objects.id].health_state == "healthy"


async def test_missing_kb_is_a_not_found(catalog: CatalogService) -> None:
    with pytest.raises(NotFound):
        await catalog.get_kb(uuid4())


async def test_parser_chunker_specs_round_trip_with_parent_links(
    admin: Principal, catalog: CatalogService, kb
) -> None:
    """TC-M07-08/12, NFR-R-06: actual parser/chunker output survives catalog writes and replay."""
    import tiktoken

    from cairn.embedding.tokenizers import TiktokenTokenizer
    from cairn.ingestion.base import ParseContext
    from cairn.ingestion.chunkers import DocumentChunker
    from cairn.ingestion.registry import get_parser_registry

    registration = await _upload(catalog, admin, kb.id, name="guide.md")
    assert registration.document is not None
    document_id = registration.document.id
    parsed = await get_parser_registry().parse(
        ("# Guide\n\n## Topic\n\n" + "知识检索保留引用。" * 40).encode(),
        mime="text/markdown",
        ctx=ParseContext(language="zh", source_url="https://example.org/guide"),
    )
    tokenizer = TiktokenTokenizer(
        tiktoken.Encoding(
            name="chunk-db-byte",
            pat_str=r"(?s).",
            mergeable_ranks={bytes([token_id]): token_id for token_id in range(256)},
            special_tokens={},
        )
    )
    chunker = DocumentChunker(document_id=document_id, tokenizer=tokenizer, index_version=1)
    cfg = ChunkConfig(child_tokens=128, child_overlap=16, parent_tokens=384)
    specs = await chunker.chunk(parsed, cfg)
    assert await catalog.replace_chunks(kb.id, document_id, 1, specs) == len(specs)
    saved = await catalog.list_chunks(kb.id, document_id, index_version=1, limit=200)
    assert len(saved) == len(specs)
    expected = {spec.id: spec for spec in specs}
    parents = {spec.id for spec in specs if not spec.metadata["embed"]}
    for item in saved:
        spec = expected[item.id]
        assert item.document_id == document_id
        assert item.parent_id == spec.parent_id
        assert item.content == spec.content
        assert item.token_count == spec.token_count
        assert item.metadata == spec.metadata
        if item.metadata["embed"]:
            assert item.parent_id in parents
    assert await catalog.replace_chunks(
        kb.id, document_id, 1, await chunker.chunk(parsed, cfg)
    ) == len(specs)
    assert await catalog.list_chunks(kb.id, document_id, index_version=1, limit=200) == saved
