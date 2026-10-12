from __future__ import annotations

import base64
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from cairn.core.modelref import ModelRef
from cairn.core.secrets import SealedCredential, SecretUnreadable, seal, unseal
from cairn.core.sparse import BM25_ENCODER_ID, SparseSpec
from cairn.embedding.sparse import ENCODER_ID

MASTER = SecretStr(base64.b64encode(b"k" * 32).decode())
OTHER_MASTER = SecretStr(base64.b64encode(b"o" * 32).decode())


def sealed_for(workspace_id=None, secret_id=None):  # type: ignore[no-untyped-def]
    workspace_id = workspace_id or uuid4()
    secret_id = secret_id or uuid4()
    return (
        seal(
            "sk-live-123",
            master_key=MASTER,
            workspace_id=workspace_id,
            purpose="model_provider",
            secret_id=secret_id,
        ),
        workspace_id,
        secret_id,
    )


def test_sealed_secret_round_trips_and_hides_plaintext() -> None:
    sealed, workspace_id, secret_id = sealed_for()
    assert b"sk-live-123" not in sealed.ciphertext + sealed.wrapped_dek
    opened = unseal(
        sealed,
        master_key=MASTER,
        workspace_id=workspace_id,
        purpose="model_provider",
        secret_id=secret_id,
    )
    assert opened.get_secret_value() == "sk-live-123"


@pytest.mark.parametrize("change", ["workspace", "purpose", "secret", "master", "tamper"])
def test_secret_is_bound_to_its_context(change: str) -> None:
    sealed, workspace_id, secret_id = sealed_for()
    kwargs = {
        "master_key": MASTER,
        "workspace_id": workspace_id,
        "purpose": "model_provider",
        "secret_id": secret_id,
    }
    if change == "workspace":
        kwargs["workspace_id"] = uuid4()
    elif change == "purpose":
        kwargs["purpose"] = "webhook"
    elif change == "secret":
        kwargs["secret_id"] = uuid4()
    elif change == "master":
        kwargs["master_key"] = OTHER_MASTER
    else:
        sealed = replace(
            sealed, ciphertext=bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
        )
    with pytest.raises(SecretUnreadable):
        unseal(sealed, **kwargs)  # type: ignore[arg-type]


def test_transport_form_opens_only_in_its_workspace() -> None:
    sealed, workspace_id, secret_id = sealed_for()
    credential = SealedCredential.from_sealed(sealed, secret_id=secret_id, purpose="model_provider")
    restored = SealedCredential.model_validate_json(credential.model_dump_json())
    assert "sk-live-123" not in restored.model_dump_json()
    assert restored.open(MASTER, workspace_id).get_secret_value() == "sk-live-123"
    with pytest.raises(SecretUnreadable):
        restored.open(MASTER, uuid4())


def test_invalid_master_key_is_refused() -> None:
    with pytest.raises(SecretUnreadable):
        seal(
            "x",
            master_key=SecretStr("not base64!"),
            workspace_id=uuid4(),
            purpose="p",
            secret_id=uuid4(),
        )


def sparse_model(**overrides: object) -> ModelRef:
    base = ModelRef(
        id=uuid4(),
        provider_family="bge_m3",
        model_key="BAAI/bge-m3",
        capability="embedding",
        dimension=1024,
        max_input_tokens=8192,
        tokenizer_id="bge-m3",
        sparse=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_sparse_spec_modifier_and_identity() -> None:
    bm25 = SparseSpec()
    assert bm25.modifier == "idf" and bm25.encoder_id == BM25_ENCODER_ID == ENCODER_ID
    model = sparse_model()
    learned = SparseSpec(kind="model", model=model)
    assert learned.modifier == "none"
    assert learned.encoder_id == f"model:{model.id}"
    assert SparseSpec.model_validate_json(learned.model_dump_json()) == learned


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "bm25", "model": "set"},
        {"kind": "model"},
        {"kind": "model", "model": "dense-only"},
    ],
)
def test_sparse_spec_rejects_incoherent_sources(payload: dict[str, object]) -> None:
    if payload.get("model") == "set":
        payload["model"] = sparse_model()
    elif payload.get("model") == "dense-only":
        payload["model"] = sparse_model(sparse=False)
    with pytest.raises(ValueError):
        SparseSpec(**payload)  # type: ignore[arg-type]
