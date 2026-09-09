from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from cairn.core.modelref import ModelRef
from cairn.embedding.errors import TokenizerUnavailable
from cairn.embedding.tokenizers import TokenizerRegistry
from cairn.ingestion.language import (
    LanguageResult,
    get_language_detector,
    resolve_language,
    tokenizer_for_embedding,
)


class StubTokenizer:
    fingerprint = "stub"

    def count(self, text: str) -> int:
        return len(text)

    def truncate(self, text: str, limit: int) -> str:
        return text[:limit]


async def test_explicit_language_is_preserved_without_detector_access() -> None:
    class FailingDetector:
        def detect(self, text: str) -> LanguageResult:
            raise AssertionError("explicit language must bypass automatic detection")

    result = await resolve_language(
        "This content is deliberately ignored.",
        explicit="zh-Hant-TW",
        detector=FailingDetector(),
    )

    assert result == LanguageResult(language="zh-Hant-TW", confidence=1.0)


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Reliable language identification uses enough ordinary English prose to avoid "
            "pretending that a short label has a precise language.",
            "en",
        ),
        ("这是一个用于测试离线语言识别的中文段落\uff0c它包含足够多的自然语言字符。", "zh"),
        ("これはオフライン言語検出を確認するための十分な長さの日本語文章です。", "ja"),
        ("이 문장은 오프라인 언어 감지가 한국어를 올바르게 찾는지 확인합니다.", "ko"),
    ],
)
async def test_supported_languages_map_deterministically(text: str, expected: str) -> None:
    result = await resolve_language(text)

    assert result.language == expected
    assert result.confidence >= 0.75


@pytest.mark.parametrize("text", ["", "OK", "12345 -- ???", "table value total"])
async def test_short_or_low_information_text_resolves_to_und(text: str) -> None:
    assert await resolve_language(text) == LanguageResult(language="und", confidence=0.0)


@pytest.mark.parametrize(
    "text",
    [
        "hello bonjour hallo hola ciao salut merci danke grazie gracias",
        "abcdef ghijkl mnopqr stuvwx yzabcdef ghijklm",
        "Этот документ описывает обработку текстовых файлов и сохранение исходной информации.",
    ],
    ids=["ambiguous", "nonsense", "unsupported-russian"],
)
async def test_ambiguous_and_unsupported_languages_are_not_forced_into_supported_tags(
    text: str,
) -> None:
    assert await resolve_language(text) == LanguageResult("und", 0.0)


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Ceci est un paragraphe suffisamment long pour identifier avec certitude "
            "la langue française utilisée dans ce document.",
            "fr",
        ),
        (
            "Dies ist ein ausreichend langer deutscher Absatz zur zuverlässigen "
            "Erkennung der Sprache dieses Dokuments.",
            "de",
        ),
    ],
)
async def test_non_english_latin_languages_are_not_defaulted_to_english(
    text: str, expected: str
) -> None:
    assert (await resolve_language(text)).language == expected


async def test_language_sampling_and_model_initialization_run_in_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    calls: list[int] = []
    samples: list[str] = []

    class RecordingDetector:
        def detect(self, text: str) -> LanguageResult:
            calls.append(threading.get_ident())
            samples.append(text)
            return LanguageResult("und", 0.0)

    def factory() -> RecordingDetector:
        calls.append(threading.get_ident())
        return RecordingDetector()

    monkeypatch.setattr("cairn.ingestion.language.get_language_detector", factory)
    await resolve_language("start" + "middle" * 20_000 + "end")
    assert all(thread != loop_thread for thread in calls)
    assert len(samples[0]) <= 20_000
    assert samples[0].startswith("start") and samples[0].endswith("end")


def test_language_detector_factory_is_a_single_offline_instance() -> None:
    assert get_language_detector() is get_language_detector()


def test_tokenizer_selection_is_bound_only_to_the_embedding_model() -> None:
    registry = TokenizerRegistry()
    tokenizer = StubTokenizer()
    registry.register("configured-tokenizer", tokenizer)
    model = ModelRef(
        id=uuid4(),
        provider_family="test",
        model_key="multilingual-embedding",
        capability="embedding",
        tokenizer_id="configured-tokenizer",
    )

    assert tokenizer_for_embedding(model, registry) is tokenizer

    missing = ModelRef(
        id=uuid4(),
        provider_family="test",
        model_key="another-embedding",
        capability="embedding",
        tokenizer_id="unknown",
    )
    with pytest.raises(TokenizerUnavailable):
        tokenizer_for_embedding(missing, registry)
