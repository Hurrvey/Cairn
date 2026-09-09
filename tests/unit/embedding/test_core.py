import json
import math
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors

from cairn.core.modelref import ModelRef
from cairn.embedding.base import Vector
from cairn.embedding.errors import (
    EmbeddingDimensionMismatch,
    EmbeddingInvalidVector,
    TokenizerUnavailable,
)
from cairn.embedding.tokenizers import HuggingFaceTokenizer, TokenizerRegistry


def test_embedding_import_does_not_initialize_control_plane_or_sqlalchemy() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import cairn.embedding; "
            "assert not any(m.startswith(('cairn.modelgw', 'sqlalchemy', 'cairn.catalog')) "
            "for m in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_vector_binary_roundtrip_and_normalization() -> None:
    vector = Vector.from_values([3, 4, 0], dim=3, normalize=True)
    assert vector.dim == 3
    assert vector.normalized
    assert math.hypot(*vector.values) == pytest.approx(1, abs=1e-6)
    assert len(vector.to_bytes()) == 12
    assert vector.to_bytes() == struct.pack("<3f", 0.6, 0.8, 0)
    restored = Vector.from_bytes(vector.to_bytes(), dim=3, normalized=True)
    assert restored.values == pytest.approx(vector.values, abs=1e-6)


@pytest.mark.parametrize("values", [[0, 0, 0], [float("nan"), 1, 0], [float("inf"), 0, 0]])
def test_normalizing_invalid_vector_is_terminal(values: list[float]) -> None:
    with pytest.raises(EmbeddingInvalidVector) as caught:
        Vector.from_values(values, dim=3, normalize=True)
    assert not caught.value.retryable


def test_dimension_mismatch_is_terminal() -> None:
    with pytest.raises(EmbeddingDimensionMismatch) as caught:
        Vector.from_values([1, 2], dim=3, normalize=False)
    assert not caught.value.retryable


def test_float32_overflow_and_corrupt_cache_are_rejected() -> None:
    with pytest.raises(EmbeddingInvalidVector):
        Vector.from_values([1e100], dim=1, normalize=False)
    with pytest.raises(EmbeddingInvalidVector):
        Vector.from_bytes(b"bad", dim=3, normalized=False)
    with pytest.raises(EmbeddingInvalidVector):
        Vector.from_bytes(struct.pack("<3f", 3, 4, 0), dim=3, normalized=True)


def test_tokenizer_counts_chinese_tokens_and_truncates_valid_utf8(
    tokenizers: TokenizerRegistry,
    model: ModelRef,
) -> None:
    tokenizer = tokenizers.for_model(model)
    text = "你好世界"
    assert tokenizer.count(text) == 12
    assert tokenizer.truncate(text, 4) == "你"
    assert tokenizer.count(tokenizer.truncate(text, 4)) <= 4
    assert tokenizer.truncate(text, 0) == ""
    assert tokenizer.truncate("hello", 20) == "hello"
    with pytest.raises(ValueError):
        tokenizer.truncate(text, -1)


def test_registry_rejects_unknown_and_duplicate_tokenizers(
    tokenizers: TokenizerRegistry,
    model: ModelRef,
) -> None:
    with pytest.raises(TokenizerUnavailable):
        TokenizerRegistry().for_model(model)
    with pytest.raises(ValueError):
        tokenizers.register("test", tokenizers.for_model(model))


def test_hf_tokenizer_preserves_source_and_accounts_for_special_tokens(tmp_path: Path) -> None:
    vocabulary = {
        word: i for i, word in enumerate(["[UNK]", "[CLS]", "[SEP]", "hello", "world", "你", "好"])
    }
    raw = Tokenizer(models.WordPiece(vocabulary, unk_token="[UNK]"))
    raw.normalizer = normalizers.BertNormalizer(lowercase=True)
    raw.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    raw.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)]
    )
    # A saved tokenizer may carry old server-side padding/truncation settings.
    raw.enable_truncation(3)
    raw.enable_padding(length=20)
    path = tmp_path / "tokenizer.json"
    raw.save(str(path))
    adapter = HuggingFaceTokenizer.from_file(path)
    assert adapter.count("HELLO world") == 4
    assert adapter.count("你好") == 4
    assert adapter.truncate("HELLO world", 3) == "HELLO"
    assert adapter.truncate("你好", 3) == "你"
    with pytest.raises(ValueError):
        adapter.truncate("hello", 1)
    assert json.loads(path.read_text())["padding"] is not None
