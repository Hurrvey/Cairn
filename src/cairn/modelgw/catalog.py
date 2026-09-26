"""Model registry.

The lookup half of M10. Invocation lands in Phase 3; this exists now so a
knowledge base can validate its embedding configuration at creation rather than
discovering a mismatch mid-ingest.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.core.cache import Cache, get_cache
from cairn.core.config import get_settings
from cairn.core.db import session_scope, transaction
from cairn.core.errors import Conflict, NotFound, ValidationFailed
from cairn.core.ids import new_uuid
from cairn.core.logging import get_logger
from cairn.core.provider_runtime import (
    HOSTED_FAMILIES,
    PROVIDER_FAMILIES,
    PROVIDER_RUNTIME_TTL,
    SPARSE_FAMILIES,
    ProviderRuntimeProjection,
    provider_runtime_key,
    provider_runtime_tombstone_key,
)
from cairn.core.secrets import SealedCredential, SealedSecret, seal, unseal
from cairn.modelgw.dto import (
    Capability,
    EmbeddingRuntimeRef,
    ModelRef,
    ModelView,
    ProviderView,
    RegisterModelSpec,
)
from cairn.modelgw.models import Model, ModelProvider, Secret

__all__ = ["SUPPORTED_FAMILIES", "ModelCatalog", "get_model_catalog"]

log = get_logger(__name__)
_RUNTIME_CACHE_TIMEOUT_S = 2.0
#: ``secret.purpose`` for provider API keys; part of the ciphertext's associated data.
PROVIDER_SECRET_PURPOSE = "model_provider"  # noqa: S105 - a label, not a credential

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
) | frozenset(PROVIDER_FAMILIES)


class ModelCatalog:
    def __init__(self, *, cache: Cache | None = None) -> None:
        self._cache = cache

    @property
    def cache(self) -> Cache:
        return self._cache or get_cache()

    # --- providers ----------------------------------------------------------

    async def create_provider(
        self,
        workspace_id: UUID,
        *,
        name: str,
        family: str,
        base_url: str | None = None,
        config: dict[str, object] | None = None,
        api_key: SecretStr | None = None,
    ) -> ProviderView:
        if family not in SUPPORTED_FAMILIES:
            raise ValidationFailed(
                f"Unknown provider family {family!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_FAMILIES))}."
            )
        if family in HOSTED_FAMILIES and api_key is None:
            raise ValidationFailed(f"The {family} provider requires an API key.")
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
            if api_key is not None:
                provider.secret_ref = await _store_secret(session, workspace_id, api_key)
            session.add(provider)
            await session.flush()
            return _provider_view(provider, model_count=0)

    async def set_provider_credentials(
        self, workspace_id: UUID, provider_id: UUID, api_key: SecretStr | None
    ) -> ProviderView:
        """Replace (or, where allowed, remove) a provider's API key."""
        async with transaction() as session:
            provider = await session.get(ModelProvider, provider_id, with_for_update=True)
            if provider is None or provider.workspace_id != workspace_id:
                raise NotFound("Provider not found.")
            if api_key is None and provider.family in HOSTED_FAMILIES:
                raise ValidationFailed(f"The {provider.family} provider requires an API key.")
            previous = provider.secret_ref
            provider.secret_ref = (
                await _store_secret(session, workspace_id, api_key) if api_key is not None else None
            )
            await session.flush()
            if previous is not None:
                old = await session.get(Secret, previous)
                if old is not None:
                    await session.delete(old)
            count = int(
                await session.scalar(
                    select(func.count(Model.id)).where(Model.provider_id == provider.id)
                )
                or 0
            )
            view = _provider_view(provider, model_count=count)
        log.info("modelgw.provider_credentials_replaced", provider_id=str(provider_id))
        return view

    async def provider_api_key(self, provider_id: UUID) -> SecretStr | None:
        """Decrypt a provider's key for a worker. Never logged, never returned by the API."""
        async with session_scope() as session:
            provider = await session.get(ModelProvider, provider_id)
            if provider is None:
                raise NotFound("Provider not found.")
            if provider.secret_ref is None:
                return None
            secret = await session.get(Secret, provider.secret_ref)
            if secret is None:
                raise NotFound("Provider credential not found.")
        return unseal(
            _sealed(secret),
            master_key=get_settings().master_key,
            workspace_id=secret.workspace_id,
            purpose=secret.purpose,
            secret_id=secret.id,
        )

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
                provider.id: int(
                    await session.scalar(
                        select(func.count(Model.id)).where(Model.provider_id == provider.id)
                    )
                    or 0
                )
                for provider in providers
            }
        return [
            _provider_view(provider, model_count=counts.get(provider.id, 0))
            for provider in providers
        ]

    async def delete_provider(self, workspace_id: UUID, provider_id: UUID) -> None:
        try:
            async with transaction() as session:
                provider = await session.get(ModelProvider, provider_id, with_for_update=True)
                if provider is None or provider.workspace_id != workspace_id:
                    raise NotFound("Provider not found.")
                in_use = await session.scalar(
                    select(exists().where(Model.provider_id == provider_id))
                )
                if in_use:
                    raise Conflict("Delete the provider's models before deleting the provider.")
                async with asyncio.timeout(_RUNTIME_CACHE_TIMEOUT_S):
                    await self.cache.delete(provider_runtime_key(provider_id))
                    await self.cache.set(
                        provider_runtime_tombstone_key(provider_id),
                        b"deleted",
                        PROVIDER_RUNTIME_TTL,
                    )
                secret_ref = provider.secret_ref
                await session.delete(provider)
                if secret_ref is not None:
                    await session.flush()
                    secret = await session.get(Secret, secret_ref)
                    if secret is not None:
                        await session.delete(secret)
        except IntegrityError as exc:
            raise Conflict("The provider is still referenced by a model.") from exc

    async def runtime_provider_ids(self, *, after: UUID | None, limit: int) -> list[UUID]:
        async with session_scope() as session:
            stmt = select(ModelProvider.id).where(
                ModelProvider.is_enabled.is_(True),
                ModelProvider.family.in_(PROVIDER_FAMILIES),
            )
            if after is not None:
                stmt = stmt.where(ModelProvider.id > after)
            return list((await session.scalars(stmt.order_by(ModelProvider.id).limit(limit))).all())

    async def publish_provider_runtime(self, provider_id: UUID) -> bool:
        async with transaction() as session:
            provider = await session.get(ModelProvider, provider_id, with_for_update=True)
            if provider is None or not provider.is_enabled:
                async with asyncio.timeout(_RUNTIME_CACHE_TIMEOUT_S):
                    await self.cache.delete(provider_runtime_key(provider_id))
                    await self.cache.set(
                        provider_runtime_tombstone_key(provider_id),
                        b"deleted",
                        PROVIDER_RUNTIME_TTL,
                    )
                return False
            secret = (
                await session.get(Secret, provider.secret_ref)
                if provider.secret_ref is not None
                else None
            )
            projection = _provider_runtime_projection(provider, secret)
            async with asyncio.timeout(_RUNTIME_CACHE_TIMEOUT_S):
                await self.cache.set(
                    provider_runtime_key(provider_id),
                    projection.model_dump_json().encode(),
                    PROVIDER_RUNTIME_TTL,
                )
                await self.cache.delete(provider_runtime_tombstone_key(provider_id))
            return True

    async def invalidate_provider_runtime(self, provider_id: UUID) -> None:
        async with asyncio.timeout(_RUNTIME_CACHE_TIMEOUT_S):
            await self.cache.delete(provider_runtime_key(provider_id))
            await self.cache.set(
                provider_runtime_tombstone_key(provider_id), b"deleted", PROVIDER_RUNTIME_TTL
            )

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

            if spec.sparse and provider.family not in SPARSE_FAMILIES:
                raise ValidationFailed(
                    f"{provider.family} models do not return sparse vectors; only "
                    f"{', '.join(sorted(SPARSE_FAMILIES))} providers can."
                )
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
                sparse=spec.sparse,
                send_dimension=spec.send_dimension,
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

    async def delete_model(self, workspace_id: UUID, model_id: UUID) -> None:
        try:
            async with transaction() as session:
                model = await session.get(Model, model_id, with_for_update=True)
                if model is None or model.workspace_id != workspace_id:
                    raise NotFound("Model not found.")
                await session.delete(model)
        except IntegrityError as exc:
            raise Conflict("The model is still referenced by a knowledge base.") from exc

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
            provider_id=provider.id,
            dynamic_provider=True,
            sparse=model.sparse,
            send_dimension=model.send_dimension,
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

    async def get_embedding_runtime(
        self, model_id: UUID, *, workspace_id: UUID | None = None
    ) -> EmbeddingRuntimeRef:
        async with session_scope() as session:
            model = await session.get(Model, model_id)
            if (
                model is None
                or (workspace_id is not None and model.workspace_id != workspace_id)
                or model.capability != "embedding"
                or not model.is_enabled
            ):
                raise NotFound("Embedding model not found.")
            provider = await session.get(ModelProvider, model.provider_id)
            if provider is None or not provider.is_enabled:
                raise NotFound("Embedding provider not found.")
            secret = (
                await session.get(Secret, provider.secret_ref)
                if provider.secret_ref is not None
                else None
            )
        return EmbeddingRuntimeRef(
            model=await self.get_ref(model_id),
            provider_id=provider.id,
            provider_family=provider.family,
            base_url=provider.base_url,
            config=dict(provider.config or {}),
            has_credentials=provider.secret_ref is not None,
            credential=(
                SealedCredential.from_sealed(
                    _sealed(secret), secret_id=secret.id, purpose=secret.purpose
                )
                if secret is not None
                else None
            ),
            workspace_id=provider.workspace_id,
        )

    async def provider_runtime(
        self, workspace_id: UUID, provider_id: UUID
    ) -> ProviderRuntimeProjection:
        """The same projection the data plane receives, for probing a provider."""
        async with session_scope() as session:
            provider = await session.get(ModelProvider, provider_id)
            if provider is None or provider.workspace_id != workspace_id:
                raise NotFound("Provider not found.")
            secret = (
                await session.get(Secret, provider.secret_ref)
                if provider.secret_ref is not None
                else None
            )
            return _provider_runtime_projection(provider, secret)

    async def provider_family(self, workspace_id: UUID, provider_id: UUID) -> str:
        async with session_scope() as session:
            provider = await session.get(ModelProvider, provider_id)
            if provider is None or provider.workspace_id != workspace_id:
                raise NotFound("Provider not found.")
            return provider.family

    async def set_model_health(self, workspace_id: UUID, model_id: UUID, healthy: bool) -> None:
        from cairn.core.time import utcnow

        async with transaction() as session:
            model = await session.get(Model, model_id, with_for_update=True)
            if model is None or model.workspace_id != workspace_id:
                raise NotFound("Model not found.")
            model.health_state = "healthy" if healthy else "unavailable"
            model.checked_at = utcnow()


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
        tokenizer_id=model.tokenizer_id,
        normalize=model.normalize,
        query_prefix=model.query_prefix,
        optimal_batch_size=model.optimal_batch_size,
        is_enabled=model.is_enabled,
        health_state=model.health_state,
        checked_at=model.checked_at,
        sparse=model.sparse,
        send_dimension=model.send_dimension,
    )


def _provider_runtime_projection(
    provider: ModelProvider, secret: Secret | None = None
) -> ProviderRuntimeProjection:
    if provider.family not in PROVIDER_FAMILIES or provider.base_url is None:
        raise ValidationFailed("The provider does not have a supported runtime endpoint.")
    config = dict(provider.config or {})
    revision = config.get("binding_revision")
    allow_private = config.get("allow_private", False)
    max_batch_size = config.get("max_batch_size", 16)
    if (
        not isinstance(revision, str)
        or not revision
        or type(allow_private) is not bool
        or type(max_batch_size) is not int
    ):
        raise ValidationFailed("The provider runtime configuration is invalid.")
    return ProviderRuntimeProjection(
        id=provider.id,
        workspace_id=provider.workspace_id,
        family=provider.family,
        base_url=provider.base_url,
        allow_private=allow_private,
        binding_revision=revision,
        max_batch_size=max_batch_size,
        credential=(
            SealedCredential.from_sealed(
                _sealed(secret), secret_id=secret.id, purpose=secret.purpose
            )
            if secret is not None
            else None
        ),
    )


async def _store_secret(session: AsyncSession, workspace_id: UUID, api_key: SecretStr) -> UUID:
    value = api_key.get_secret_value().strip()
    if not value:
        raise ValidationFailed("The API key must not be empty.")
    secret_id = new_uuid()
    sealed = seal(
        value,
        master_key=get_settings().master_key,
        workspace_id=workspace_id,
        purpose=PROVIDER_SECRET_PURPOSE,
        secret_id=secret_id,
    )
    session.add(
        Secret(
            id=secret_id,
            workspace_id=workspace_id,
            purpose=PROVIDER_SECRET_PURPOSE,
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            wrapped_dek=sealed.wrapped_dek,
            key_version=sealed.key_version,
        )
    )
    await session.flush()
    return secret_id


def _sealed(secret: Secret) -> SealedSecret:
    return SealedSecret(
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_dek=secret.wrapped_dek,
        key_version=secret.key_version,
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
