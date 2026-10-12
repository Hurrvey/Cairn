from __future__ import annotations

from cairn.embedding.sparse import ENCODER_ID, encode_document, encode_query, tokenize


def weights(text: str) -> dict[int, float]:
    vector = encode_document(text)
    assert vector is not None
    return dict(zip(vector.indices, vector.values, strict=True))


def test_codes_are_indexed_whole_and_by_parts() -> None:
    tokens = tokenize("The XR-2200 controller needs firmware v4.1.")
    assert {"xr-2200", "xr", "2200", "v4.1", "v4", "1", "controller"} <= set(tokens)


def test_chinese_is_segmented_into_words_and_subwords() -> None:
    assert {"知识", "知识库", "检索"} <= set(tokenize("知识库检索"))


def test_mixed_script_and_width_are_normalised() -> None:
    assert tokenize("\uff21\uff22\uff23\uff11\uff12\uff13") == ["abc123"]
    assert {"café", "déjà-vu", "déjà", "vu"} <= set(tokenize("Café déjà-vu"))
    assert {"api", "密钥"} <= set(tokenize("API密钥"))


def test_text_without_tokens_encodes_to_none() -> None:
    assert encode_document("\uff0c\u3002!? ") is None
    assert encode_query("   ") is None


def test_encoding_is_deterministic_and_sorted() -> None:
    first = encode_document("Saturn has rings. 土星有光环。")
    assert first == encode_document("Saturn has rings. 土星有光环。")
    assert first is not None
    assert list(first.indices) == sorted(set(first.indices))
    assert all(0 <= index < 2**32 for index in first.indices)


def test_query_weights_are_one_per_unique_token() -> None:
    query = encode_query("rings rings saturn")
    assert query is not None
    assert len(query.indices) == 2
    assert set(query.values) == {1.0}


def test_term_frequency_saturates() -> None:
    doc = weights("alpha alpha alpha alpha beta")
    alpha = next(iter(weights("alpha")))
    beta = next(iter(weights("beta")))
    assert doc[beta] < doc[alpha] < 4 * doc[beta]


def test_query_tokens_hit_document_indices() -> None:
    query = encode_query("XR-2200")
    document = encode_document("The XR-2200 controller requires firmware 4.1.")
    assert query is not None and document is not None
    assert set(query.indices) <= set(document.indices)


def test_encoder_id_is_versioned() -> None:
    assert ENCODER_ID == "bm25-jieba-v1"
