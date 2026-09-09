"""Explicitly loaded model tokenizers; no remote lookup on the hot path."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Protocol

import tiktoken
from tokenizers import Tokenizer as HFTokenizer

from cairn.core.modelref import ModelRef
from cairn.embedding.errors import TokenizerUnavailable


class Tokenizer(Protocol):
    fingerprint: str

    def count(self, text: str) -> int: ...
    def truncate(self, text: str, limit: int) -> str: ...


class TiktokenTokenizer:
    def __init__(self, encoding: tiktoken.Encoding) -> None:
        self._encoding = encoding
        # A byte-level fingerprint binds cache entries to the actual vocabulary.
        digest = sha256(encoding.name.encode())
        for token_id in range(encoding.n_vocab):
            try:
                token = encoding.decode_single_token_bytes(token_id)
            except KeyError:
                continue
            digest.update(token_id.to_bytes(8, "big"))
            digest.update(len(token).to_bytes(8, "big"))
            digest.update(token)
        self.fingerprint = digest.hexdigest()

    @classmethod
    def from_encoding(cls, name: str) -> TiktokenTokenizer:
        # Explicit startup operation. tiktoken may populate its local asset cache here.
        return cls(tiktoken.get_encoding(name))

    def count(self, text: str) -> int:
        return len(self._encoding.encode_ordinary(text))

    def truncate(self, text: str, limit: int) -> str:
        if limit < 0:
            raise ValueError("token limit must be nonnegative")
        tokens = self._encoding.encode_ordinary(text)
        if len(tokens) <= limit:
            return text
        raw = b"".join(self._encoding.decode_single_token_bytes(i) for i in tokens[:limit])
        candidate = raw.decode("utf-8", errors="ignore")
        while candidate and self.count(candidate) > limit:
            candidate = candidate[:-1]
        return candidate


class HuggingFaceTokenizer:
    def __init__(self, tokenizer: HFTokenizer) -> None:
        # Own a copy: disabling saved server truncation must not mutate the caller.
        self._tokenizer = HFTokenizer.from_str(tokenizer.to_str())
        self._tokenizer.no_padding()
        self._tokenizer.no_truncation()
        self.fingerprint = sha256(self._tokenizer.to_str().encode()).hexdigest()

    @classmethod
    def from_file(cls, path: Path) -> HuggingFaceTokenizer:
        return cls(HFTokenizer.from_file(str(path)))

    def count(self, text: str) -> int:
        return len(self._tokenizer.encode(text).ids)

    def truncate(self, text: str, limit: int) -> str:
        if limit < 0 or self.count("") > limit:
            raise ValueError("token limit cannot accommodate the model's special tokens")
        encoding = self._tokenizer.encode(text)
        if len(encoding.ids) <= limit:
            return text
        # Prefix slicing preserves original casing/spacing instead of decoding a
        # normalized WordPiece sequence. Re-encode to verify the final boundary.
        budget = limit - self._tokenizer.num_special_tokens_to_add(False)
        offsets = self._tokenizer.encode(text, add_special_tokens=False).offsets
        end = offsets[budget - 1][1] if budget > 0 else 0
        candidate = text[:end].rstrip()
        while candidate and self.count(candidate) > limit:
            candidate = candidate[:-1]
        return candidate


class TokenizerRegistry:
    def __init__(self) -> None:
        self._tokenizers: dict[str, Tokenizer] = {}

    def register(self, name: str, tokenizer: Tokenizer) -> None:
        if not name or name in self._tokenizers:
            raise ValueError("tokenizer name is empty or already registered")
        self._tokenizers[name] = tokenizer

    def for_model(self, model: ModelRef) -> Tokenizer:
        if model.tokenizer_id is None or model.tokenizer_id not in self._tokenizers:
            raise TokenizerUnavailable()
        return self._tokenizers[model.tokenizer_id]
