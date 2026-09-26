"""Envelope encryption for stored credentials (NFR-SEC-02).

Each secret gets its own random data key (DEK); the DEK is wrapped by a key
encryption key (KEK) derived from ``CAIRN_MASTER_KEY``. Both layers are
AES-256-GCM, and both bind the same associated data —
``workspace:purpose:secret_id`` — so a ciphertext lifted from a database dump
and replayed under another workspace, purpose or row fails to open instead of
yielding a credential.

Only this module sees plaintext. Callers hold :class:`SealedSecret` values,
which are safe to store, cache and publish; opening one requires the master key
that every Cairn process already has and nothing else does.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from cairn.core.errors import CairnError

__all__ = [
    "KEY_VERSION",
    "SealedCredential",
    "SealedSecret",
    "SecretUnreadable",
    "seal",
    "unseal",
]

KEY_VERSION = 1
_NONCE_BYTES = 12
_KEK_INFO = b"cairn/secret-kek/v1"


class SecretUnreadable(CairnError):
    """A stored secret cannot be opened: a different master key, or tampering."""

    code = "SECRET_UNREADABLE"
    http_status = 500
    title = "Stored credential cannot be read"


@dataclass(frozen=True, slots=True)
class SealedSecret:
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    key_version: int = KEY_VERSION


class SealedCredential(BaseModel):
    """A sealed secret in transport form, for projections published to Redis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    secret_id: UUID
    purpose: str = Field(min_length=1, max_length=32)
    ciphertext: str
    nonce: str
    wrapped_dek: str
    key_version: int = Field(ge=1)

    @classmethod
    def from_sealed(
        cls, sealed: SealedSecret, *, secret_id: UUID, purpose: str
    ) -> SealedCredential:
        return cls(
            secret_id=secret_id,
            purpose=purpose,
            ciphertext=base64.b64encode(sealed.ciphertext).decode("ascii"),
            nonce=base64.b64encode(sealed.nonce).decode("ascii"),
            wrapped_dek=base64.b64encode(sealed.wrapped_dek).decode("ascii"),
            key_version=sealed.key_version,
        )

    def sealed(self) -> SealedSecret:
        try:
            return SealedSecret(
                ciphertext=base64.b64decode(self.ciphertext, validate=True),
                nonce=base64.b64decode(self.nonce, validate=True),
                wrapped_dek=base64.b64decode(self.wrapped_dek, validate=True),
                key_version=self.key_version,
            )
        except (binascii.Error, ValueError) as exc:
            raise SecretUnreadable("The published credential is malformed.") from exc

    def open(self, master_key: SecretStr, workspace_id: UUID) -> SecretStr:
        return unseal(
            self.sealed(),
            master_key=master_key,
            workspace_id=workspace_id,
            purpose=self.purpose,
            secret_id=self.secret_id,
        )


def _kek(master_key: SecretStr, key_version: int) -> bytes:
    if key_version != KEY_VERSION:
        raise SecretUnreadable(f"Unknown secret key version {key_version}.")
    try:
        material = base64.b64decode(master_key.get_secret_value(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretUnreadable("CAIRN_MASTER_KEY is not valid base64.") from exc
    if len(material) < 32:
        raise SecretUnreadable("CAIRN_MASTER_KEY must decode to at least 32 bytes.")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEK_INFO).derive(material)


def _aad(workspace_id: UUID, purpose: str, secret_id: UUID) -> bytes:
    return f"{workspace_id}:{purpose}:{secret_id}".encode()


def seal(
    plaintext: str,
    *,
    master_key: SecretStr,
    workspace_id: UUID,
    purpose: str,
    secret_id: UUID,
) -> SealedSecret:
    if not plaintext:
        raise ValueError("refusing to seal an empty secret")
    aad = _aad(workspace_id, purpose, secret_id)
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad)
    wrap_nonce = os.urandom(_NONCE_BYTES)
    wrapped = wrap_nonce + AESGCM(_kek(master_key, KEY_VERSION)).encrypt(wrap_nonce, dek, aad)
    return SealedSecret(ciphertext=ciphertext, nonce=nonce, wrapped_dek=wrapped)


def unseal(
    sealed: SealedSecret,
    *,
    master_key: SecretStr,
    workspace_id: UUID,
    purpose: str,
    secret_id: UUID,
) -> SecretStr:
    aad = _aad(workspace_id, purpose, secret_id)
    kek = _kek(master_key, sealed.key_version)
    wrap_nonce, wrapped = sealed.wrapped_dek[:_NONCE_BYTES], sealed.wrapped_dek[_NONCE_BYTES:]
    try:
        dek = AESGCM(kek).decrypt(wrap_nonce, wrapped, aad)
        plaintext = AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, aad)
    except (InvalidTag, ValueError) as exc:
        # Deliberately indistinguishable: wrong master key, wrong workspace or
        # purpose, and a modified ciphertext all look the same to a caller.
        raise SecretUnreadable("The stored credential could not be decrypted.") from exc
    return SecretStr(plaintext.decode("utf-8"))
