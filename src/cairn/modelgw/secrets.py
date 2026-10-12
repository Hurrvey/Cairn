"""Transaction-scoped encrypted secret facade for storage bindings."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from cairn.core.config import get_settings
from cairn.core.ids import new_uuid
from cairn.core.secrets import SealedCredential, SealedSecret, SecretUnreadable, seal
from cairn.modelgw.models import Secret

PURPOSE = "storage_binding"


async def store_secret(session: AsyncSession, workspace_id: UUID, plaintext: str) -> UUID:
    secret_id = new_uuid()
    value = seal(
        plaintext,
        master_key=get_settings().master_key,
        workspace_id=workspace_id,
        purpose=PURPOSE,
        secret_id=secret_id,
    )
    session.add(
        Secret(
            id=secret_id,
            workspace_id=workspace_id,
            purpose=PURPOSE,
            ciphertext=value.ciphertext,
            nonce=value.nonce,
            wrapped_dek=value.wrapped_dek,
            key_version=value.key_version,
        )
    )
    await session.flush()
    return secret_id


async def sealed_secret(
    session: AsyncSession, workspace_id: UUID, secret_id: UUID | None
) -> SealedCredential | None:
    if secret_id is None:
        return None
    row = await session.get(Secret, secret_id)
    if row is None or row.workspace_id != workspace_id or row.purpose != PURPOSE:
        raise SecretUnreadable("Storage credential is unavailable.")
    return SealedCredential.from_sealed(
        SealedSecret(
            ciphertext=row.ciphertext,
            nonce=row.nonce,
            wrapped_dek=row.wrapped_dek,
            key_version=row.key_version,
        ),
        secret_id=secret_id,
        purpose=PURPOSE,
    )


async def delete_secret(session: AsyncSession, workspace_id: UUID, secret_id: UUID | None) -> None:
    if secret_id is not None:
        row = await session.get(Secret, secret_id)
        if row is not None and row.workspace_id == workspace_id and row.purpose == PURPOSE:
            await session.delete(row)
