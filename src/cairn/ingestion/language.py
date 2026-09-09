"""Bounded offline language inference; embedding tokenizers remain model-owned."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from anyio import to_thread
from lingua import Language, LanguageDetectorBuilder

from cairn.core.modelref import ModelRef
from cairn.embedding.tokenizers import Tokenizer, TokenizerRegistry

_LANGUAGE_TAGS = {
    Language.CHINESE: "zh",
    Language.JAPANESE: "ja",
    Language.KOREAN: "ko",
    Language.ENGLISH: "en",
    Language.FRENCH: "fr",
    Language.GERMAN: "de",
    Language.SPANISH: "es",
    Language.PORTUGUESE: "pt",
    Language.ITALIAN: "it",
    Language.DUTCH: "nl",
    Language.POLISH: "pl",
    Language.CZECH: "cs",
}
_MAX_SAMPLE_CHARACTERS = 20_000


@dataclass(frozen=True, slots=True)
class LanguageResult:
    language: str
    confidence: float


class LanguageDetector(Protocol):
    def detect(self, text: str) -> LanguageResult: ...


class OfflineLanguageDetector:
    """Lingua scores are relative evidence, not calibrated correctness probabilities."""

    def __init__(self) -> None:
        self._detector = LanguageDetectorBuilder.from_all_languages().build()

    def detect(self, text: str) -> LanguageResult:
        sample = _sample(text)
        if sum(character.isalpha() for character in sample) < 20:
            return LanguageResult("und", 0.0)
        scores = self._detector.compute_language_confidence_values(sample)
        if not scores:
            return LanguageResult("und", 0.0)
        winner = scores[0]
        runner_up = scores[1].value if len(scores) > 1 else 0.0
        language = _LANGUAGE_TAGS.get(winner.language)
        if language is None or winner.value < 0.75 or winner.value - runner_up < 0.15:
            return LanguageResult("und", 0.0)
        return LanguageResult(language, winner.value)


@lru_cache(maxsize=1)
def get_language_detector() -> OfflineLanguageDetector:
    """Build bundled models only; no downloads or network configuration are used."""
    return OfflineLanguageDetector()


async def resolve_language(
    text: str,
    explicit: str = "und",
    detector: LanguageDetector | None = None,
) -> LanguageResult:
    """Preserve explicit tags and run automatic inference outside the event loop."""
    if explicit != "und":
        return LanguageResult(explicit, 1.0)

    def detect() -> LanguageResult:
        selected = detector if detector is not None else get_language_detector()
        return selected.detect(_sample(text))

    return await to_thread.run_sync(detect)


def tokenizer_for_embedding(model: ModelRef, registry: TokenizerRegistry) -> Tokenizer:
    """Resolve the configured embedding tokenizer without any language fallback."""
    if model.capability != "embedding":
        raise ValueError("Tokenizer selection requires an embedding model.")
    return registry.for_model(model)


def _sample(text: str) -> str:
    if len(text) <= _MAX_SAMPLE_CHARACTERS:
        return text
    section = (_MAX_SAMPLE_CHARACTERS - 2) // 3
    middle = (len(text) - section) // 2
    return "\n".join((text[:section], text[middle : middle + section], text[-section:]))
