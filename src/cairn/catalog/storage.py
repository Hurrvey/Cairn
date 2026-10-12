"""Construct an object-store reference without exposing plaintext credentials."""

from sqlalchemy.ext.asyncio import AsyncSession

from cairn.catalog.models import StorageBinding
from cairn.modelgw.secrets import sealed_secret
from cairn.objectstore.registry import ObjectBindingRef


async def object_binding(session: AsyncSession, binding: StorageBinding) -> ObjectBindingRef:
    return ObjectBindingRef(
        binding.id,
        binding.driver,
        dict(binding.config or {}),
        credential=await sealed_secret(session, binding.workspace_id, binding.secret_ref),
        workspace_id=binding.workspace_id,
    )
