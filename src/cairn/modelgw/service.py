"""Application service for safe model/provider administration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from cairn.core.config import Settings, get_settings
from cairn.core.errors import Conflict, ValidationFailed
from cairn.core.logging import get_logger
from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import ProviderRuntimeProjection
from cairn.core.tokenizer_config import configured_tokenizer_ids
from cairn.modelgw.catalog import ModelCatalog, get_model_catalog
from cairn.modelgw.dto import ModelView, ProviderView, RegisterModelSpec
from cairn.modelgw.schemas import (
    CreateModelRequest,
    CreateProviderRequest,
    ProviderCredentialsRequest,
)

__all__ = ["ModelManagementService", "ModelProbe", "ModelTestResult", "ModelUsage"]

log = get_logger(__name__)


class ModelUsage(Protocol):
    async def model_is_referenced(self, workspace_id: UUID, model_id: UUID) -> bool: ...


class ProbeOutcome(Protocol):
    @property
    def dimensions(self) -> int: ...
    @property
    def tokens(self) -> int: ...
    @property
    def sparse_terms(self) -> int | None: ...


class ModelProbe(Protocol):
    async def probe(
        self,
        model: ModelRef,
        provider_runtime: ProviderRuntimeProjection,
        tokenizer_path: Path,
    ) -> ProbeOutcome: ...

    async def detect_dimension(
        self, model: ModelRef, provider_runtime: ProviderRuntimeProjection
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class ModelTestResult:
    healthy: bool
    dimensions: int
    tokens: int
    sparse_terms: int | None = None


class ModelManagementService:
    def __init__(
        self,
        *,
        catalog: ModelCatalog | None = None,
        usage: ModelUsage | None = None,
        probe: ModelProbe | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._catalog = catalog or get_model_catalog()
        self._usage = usage
        self._probe = probe
        self._settings = settings or get_settings()

    async def create_provider(
        self, workspace_id: UUID, request: CreateProviderRequest
    ) -> ProviderView:
        provider = await self._catalog.create_provider(
            workspace_id,
            name=request.name,
            family=request.family,
            base_url=request.base_url,
            config=request.config.model_dump(),
            api_key=request.api_key,
        )
        await self._publish_safely(provider.id)
        return provider

    async def replace_credentials(
        self, workspace_id: UUID, provider_id: UUID, request: ProviderCredentialsRequest
    ) -> ProviderView:
        provider = await self._catalog.set_provider_credentials(
            workspace_id, provider_id, request.api_key
        )
        # A new projection fingerprint makes every data process replace its client.
        await self._publish_safely(provider.id)
        return provider

    async def list_providers(self, workspace_id: UUID) -> list[ProviderView]:
        return await self._catalog.list_providers(workspace_id)

    async def delete_provider(self, workspace_id: UUID, provider_id: UUID) -> None:
        await self._catalog.delete_provider(workspace_id, provider_id)

    async def create_model(self, workspace_id: UUID, request: CreateModelRequest) -> ModelView:
        tokenizers = configured_tokenizer_ids(self._settings.retrieval)
        if request.tokenizer_id not in tokenizers:
            raise ValidationFailed("The selected tokenizer is not configured in this deployment.")
        from cairn.core.ids import decode_id

        provider_id = decode_id("prov", request.provider_id)
        dimension = request.dimension
        if dimension is None:
            dimension = await self._detect_dimension(workspace_id, provider_id, request)
        model = await self._catalog.register_model(
            workspace_id,
            RegisterModelSpec(
                provider_id=provider_id,
                model_key=request.model_key,
                display_name=request.display_name,
                capability=request.capability,
                dimension=dimension,
                max_input_tokens=request.max_input_tokens,
                normalize=request.normalize,
                query_prefix=request.query_prefix,
                optimal_batch_size=request.optimal_batch_size,
                tokenizer_id=request.tokenizer_id,
                sparse=request.sparse,
                send_dimension=request.send_dimension,
            ),
        )
        await self._publish_safely(model.provider_id)
        return model

    async def _detect_dimension(
        self, workspace_id: UUID, provider_id: UUID, request: CreateModelRequest
    ) -> int:
        if self._probe is None:
            raise ValidationFailed("Enter the model's dimension; automatic detection is off.")
        runtime = await self._catalog.provider_runtime(workspace_id, provider_id)
        stub = ModelRef(
            id=provider_id,
            provider_family=runtime.family,
            model_key=request.model_key,
            capability="embedding",
        )
        try:
            return await self._probe.detect_dimension(stub, runtime)
        except Exception as exc:
            log.warning(
                "modelgw.dimension_detection_failed",
                provider_id=str(provider_id),
                error=type(exc).__name__,
            )
            raise ValidationFailed(
                "The provider did not return an embedding for this model. Check the model name "
                "and API key, or enter the dimension yourself."
            ) from exc

    async def _publish_safely(self, provider_id: UUID) -> None:
        try:
            await self._catalog.publish_provider_runtime(provider_id)
        except Exception as exc:
            log.warning(
                "modelgw.provider_runtime_publish_failed",
                provider_id=str(provider_id),
                error=type(exc).__name__,
            )

    async def list_models(self, workspace_id: UUID) -> list[ModelView]:
        return await self._catalog.list_models(workspace_id)

    async def delete_model(self, workspace_id: UUID, model_id: UUID) -> None:
        if self._usage is not None and await self._usage.model_is_referenced(
            workspace_id, model_id
        ):
            raise Conflict("This model is referenced by a knowledge base or index snapshot.")
        await self._catalog.delete_model(workspace_id, model_id)

    async def test_model(self, workspace_id: UUID, model_id: UUID) -> ModelTestResult:
        runtime = await self._catalog.get_embedding_runtime(model_id, workspace_id=workspace_id)
        model = runtime.model
        if (
            self._probe is None
            or runtime.base_url is None
            or model.capability != "embedding"
            or model.dimension is None
            or model.max_input_tokens is None
            or model.tokenizer_id is None
        ):
            raise ValidationFailed("The embedding model runtime is incomplete or unsupported.")
        tokenizer_path = self._settings.retrieval.tokenizer_files.get(model.tokenizer_id)
        if tokenizer_path is None:
            raise ValidationFailed("The model tokenizer file is not configured for retrieval.")
        config = dict(runtime.config)
        projection = ProviderRuntimeProjection(
            id=runtime.provider_id,
            workspace_id=workspace_id,
            family=runtime.provider_family,
            base_url=runtime.base_url,
            allow_private=config.get("allow_private", False),
            binding_revision=config.get("binding_revision", ""),
            max_batch_size=config.get("max_batch_size", 16),
            credential=runtime.credential,
        )
        try:
            outcome = await self._probe.probe(model, projection, Path(tokenizer_path))
        except Exception:
            await self._catalog.set_model_health(workspace_id, model_id, healthy=False)
            raise
        await self._catalog.set_model_health(workspace_id, model_id, healthy=True)
        return ModelTestResult(
            healthy=True,
            dimensions=outcome.dimensions,
            tokens=outcome.tokens,
            sparse_terms=outcome.sparse_terms,
        )
