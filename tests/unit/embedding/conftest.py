from dataclasses import replace
from uuid import uuid4

import pytest
import tiktoken

from cairn.core.modelref import ModelRef
from cairn.embedding.tokenizers import TiktokenTokenizer, TokenizerRegistry


@pytest.fixture
def tokenizers() -> TokenizerRegistry:
    # Actual tiktoken BPE with an offline byte vocabulary: no model download.
    encoding = tiktoken.Encoding(
        name="test-byte",
        pat_str=r"(?s).",
        mergeable_ranks={bytes([i]): i for i in range(256)},
        special_tokens={},
    )
    registry = TokenizerRegistry()
    registry.register("test", TiktokenTokenizer(encoding))
    return registry


@pytest.fixture
def model() -> ModelRef:
    return ModelRef(
        id=uuid4(),
        provider_family="tei",
        model_key="test-model",
        capability="embedding",
        dimension=3,
        max_input_tokens=100,
        normalize=True,
        optimal_batch_size=3,
        tokenizer_id="test",
    )


@pytest.fixture
def unnormalized_model(model: ModelRef) -> ModelRef:
    return replace(model, normalize=False)
