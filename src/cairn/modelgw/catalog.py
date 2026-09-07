"""Model registry.

The lookup half of M10. Invocation lands in Phase 3; this exists now so a
knowledge base can validate its embedding configuration at creation rather than
discovering a mismatch mid-ingest.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from cairn.core.db import session_scope, transaction
from cairn.core.errors import Conflict, NotFound, ValidationFailed
from cairn.core.logging import get_logger
from cairn.modelgw.dto import Capability, ModelRef, ModelView, ProviderView, RegisterModelSpec
from cairn.modelgw.models import Model, ModelProvider

__all__ = ["SUPPORTED_FAMILIES", "ModelCatalog", "get_model_catalog"]

log = get_logger(__name__)

#: Provider families the gateway will support. Registration accepts any of them
#: now; the adapters that actually call them arrive with M10.
SUPPORTED_FAMILIES: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "google",
        "dashscope",
        "deepseek",
        "ollama",
        "vllm",
        "tei",
        "infinity",
        "cohere",
        "jina",
        "openai_compatible",
    }
)


class ModelCatalog:
    # --- providers ----------------------------------------------------------

    async def create_provider(
        self,
        workspace_id: UUID,
        *,
        name: str,
        family: str,
        base_url: str | None = None,
        config: dict[str, object] | None = None,
    ) -> ProviderView:
        if family not in SUPPORTED_FAMILIES:
            raise ValidationFailed(
                f"Unknown provider family {family!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_FAMILIES))}."
            )
        async with transaction() as session:
            existing = await session.scalar(
                select(ModelProvider).where(
                    ModelProvider.workspace_id == workspace_id, ModelProvider.name == name
                )
            )
            if existing is not None:
                raise Conflict(f"A provider named {name!r} already exists.")

            provider = ModelProvider(
                workspace_id=workspace_id,
                name=name,
                family=family,
                base_url=base_url,
                config=dict(config or {}),
            )
            session.add(provider)
            await session.flush()
            return _provider_view(provider, model_count=0)

    async def list_providers(self, workspace_id: UUID) -> list[ProviderView]:
        async with session_scope() as session:
            providers = list(
                (
                    await session.scalars(
                        select(ModelProvider).where(ModelProvider.workspace_id == workspace_id)
                    )
                ).all()
            )
            counts = {
                provider.id: await session.scalar(
                    select(Model.id).where(Model.provider_id == provider.id).limit(1)
                )
                for provider in providers
            }
        return [
            _provider_view(provider, model_count=1 if counts.get(provider.id) else 0)
            for provider in providers
        ]

    # --- models -------------------------------------------------------------

    async def register_model(self, workspace_id: UUID, spec: RegisterModelSpec) -> ModelView:
        if spec.capability == "embedding" and not spec.dimension:
            # Enforced here as well as by a table constraint: the error message
            # can say *why*, and a knowledge base configured against a
            # dimensionless model would fail at the first upsert instead.
            raise ValidationFailed(
                "An embedding model must declare its dimension. It becomes the "
                "knowledge base's immutable vector width."
            )

        async with transaction() as session:
            provider = await session.get(ModelProvider, spec.provider_id)
            if provider is None or provider.workspace_id != workspace_id:
                raise NotFound("Provider not found.")

            existing = await session.scalar(
                select(Model).where(
                    Model.provider_id == spec.provider_id, Model.model_key == spec.model_key
                )
            )
            if existing is not None:
                raise Conflict(
                    f"{spec.model_key!r} is already registered on provider {provider.name!r}."
                )

            model = Model(
                workspace_id=workspace_id,
                provider_id=spec.provider_id,
                model_key=spec.model_key,
                display_name=spec.display_name,
                capability=spec.capability,
                dimension=spec.dimension,
                max_input_tokens=spec.max_input_tokens,
                normalize=spec.normalize,
                query_prefix=spec.query_prefix,
                optimal_batch_size=spec.optimal_batch_size,
                tokenizer_id=spec.tokenizer_id,
            )
            session.add(model)
            await session.flush()
            log.info(
                "modelgw.model_registered",
                model_key=spec.model_key,
                capability=spec.capability,
                dimension=spec.dimension,
            )
            return _model_view(model, provider)

    async def list_models(
        self, workspace_id: UUID, capability: Capability | None = None
    ) -> list[ModelView]:
        async with session_scope() as session:
            stmt = select(Model).where(Model.workspace_id == workspace_id)
            if capability is not None:
                stmt = stmt.where(Model.capability == capability)
            models = list((await session.scalars(stmt.order_by(Model.display_name))).all())
            providers = {
                provider.id: provider
                for provider in (
                    await session.scalars(
                        select(ModelProvider).where(ModelProvider.workspace_id == workspace_id)
                    )
                ).all()
            }
        return [_model_view(model, providers[model.provider_id]) for model in models]

    async def get_ref(self, model_id: UUID) -> ModelRef:
        """The facade other modules call. Never returns credentials."""
        async with session_scope() as session:
            model = await session.get(Model, model_id)
            if model is None:
                raise NotFound("Model not found.")
            provider = await session.get(ModelProvider, model.provider_id)
            if provider is None:  # pragma: no cover — FK guarantees this
                raise NotFound("Model provider not found.")

        return ModelRef(
            id=model.id,
            provider_family=provider.family,
            model_key=model.model_key,
            capability=model.capability,  # type: ignore[arg-type]
            dimension=model.dimension,
            max_input_tokens=model.max_input_tokens,
            normalize=model.normalize,
            query_prefix=model.query_prefix,
            optimal_batch_size=model.optimal_batch_size,
            tokenizer_id=model.tokenizer_id,
        )

    async def require_embedding_model(self, workspace_id: UUID, model_id: UUID) -> ModelRef:
        """Validate a model is usable as a knowledge base's embedding model.

        Every failure here is one that would otherwise surface much later, after
        documents have been uploaded and a namespace created.
        """
        async with session_scope() as session:
            model = await session.get(Model, model_id)

        if model is None or model.workspace_id != workspace_id:
            raise NotFound("Embedding model not found.")
        if model.capability != "embedding":
            raise ValidationFailed(
                f"{model.display_name!r} is a {model.capability} model; a knowledge "
                "base needs an embedding model."
            )
        if not model.is_enabled:
            raise ValidationFailed(f"{model.display_name!r} is disabled.")
        if not model.dimension:  # pragma: no cover — table constraint prevents this
            raise ValidationFailed(f"{model.display_name!r} has no declared dimension.")

        return await self.get_ref(model_id)


def _provider_view(provider: ModelProvider, *, model_count: int) -> ProviderView:
    return ProviderView(
        id=provider.id,
        name=provider.name,
        family=provider.family,
        base_url=provider.base_url,
        is_enabled=provider.is_enabled,
        # Presence only. The credential itself never leaves the gateway.
        has_credentials=provider.secret_ref is not None,
        model_count=model_count,
    )


def _model_view(model: Model, provider: ModelProvider) -> ModelView:
    return ModelView(
        id=model.id,
        provider_id=provider.id,
        provider_name=provider.name,
        provider_family=provider.family,
        model_key=model.model_key,
        display_name=model.display_name,
        capability=model.capability,  # type: ignore[arg-type]
        dimension=model.dimension,
        max_input_tokens=model.max_input_tokens,
        is_enabled=model.is_enabled,
        health_state=model.health_state,
        checked_at=model.checked_at,
    )


_catalog: ModelCatalog | None = None


def get_model_catalog() -> ModelCatalog:
    global _catalog
    if _catalog is None:
        _catalog = ModelCatalog()
    return _catalog


def reset_model_catalog() -> None:
    global _catalog
    _catalog = None
