"""Deterministic, source-preserving chunking using the embedding model tokenizer."""

from __future__ import annotations

import json
import unicodedata
from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from typing import Literal, Protocol
from uuid import UUID, uuid5

from anyio import to_thread

from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import ChunkSpec
from cairn.core.logging import get_logger
from cairn.embedding.tokenizers import Tokenizer
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.custom import (
    CustomChunkExecutor,
    CustomChunkLimits,
    CustomChunkScope,
    FunctionVersionRef,
    validate_custom_chunks,
)
from cairn.ingestion.errors import ChunkBudgetExceeded, ChunkUnsupportedStrategy
from cairn.ingestion.metrics import chunk_duration, chunk_results
from cairn.ingestion.semantic import SemanticBoundaryDetector, SemanticEmbedding

log = get_logger(__name__)
_STRATEGIES = frozenset({"fixed", "recursive", "markdown", "parent_child"})
_SENTENCE_SEPARATORS = ("。", "\uff01", "\uff1f", "\uff1b", ". ", "! ", "? ", "; ")


class Chunker(Protocol):
    """A chunker bound to document/index identity and an explicitly loaded tokenizer."""

    async def chunk(self, doc: ParsedDocument, cfg: ChunkConfig) -> list[ChunkSpec]:
        """Produce catalog write specifications, including unembedded parents."""
        ...


@dataclass(frozen=True, slots=True)
class _Source:
    block: Block
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Section:
    text: str
    sources: tuple[_Source, ...]
    heading_path: tuple[str, ...]
    atomic: bool = False


@dataclass(frozen=True, slots=True)
class _Window:
    section: _Section
    start: int
    end: int

    @property
    def body(self) -> str:
        return self.section.text[self.start : self.end]

    @property
    def sources(self) -> tuple[_Source, ...]:
        first = bisect_right(self.section.sources, self.start, key=lambda source: source.end)
        last = bisect_left(self.section.sources, self.end, key=lambda source: source.start)
        return self.section.sources[first:last]


def _sections(doc: ParsedDocument, cfg: ChunkConfig) -> list[_Section]:
    """Keep heading changes and protected tables as hard, non-overlapping boundaries."""
    sections: list[_Section] = []
    texts: list[str] = []
    sources: list[_Source] = []
    path: tuple[str, ...] = ()
    position = 0

    def flush() -> None:
        nonlocal position
        if sources:
            sections.append(_Section("\n\n".join(texts), tuple(sources), path))
            texts.clear()
            sources.clear()
            position = 0

    for block in doc.blocks:
        if not block.text.strip():
            continue
        if block.heading_path != path or block.kind == "heading":
            flush()
        path = block.heading_path
        if block.kind == "table" and cfg.keep_tables_intact:
            flush()
            sections.append(
                _Section(
                    block.text,
                    (_Source(block, 0, len(block.text)),),
                    path,
                    atomic=True,
                )
            )
            continue
        start = position + (2 if texts else 0)
        sources.append(_Source(block, start, start + len(block.text)))
        texts.append(block.text)
        position = start + len(block.text)
    flush()
    return sections


class DocumentChunker:
    """Four built-in strategies; IDs are stable within a document and index version.

    Identity is bound here because ParsedDocument contains format data, not catalog
    identity. No database lookup, tokenizer download or model guessing occurs.
    """

    def __init__(
        self,
        *,
        document_id: UUID,
        tokenizer: Tokenizer,
        index_version: int = 1,
        max_input_tokens: int | None = None,
        semantic: SemanticEmbedding | None = None,
        custom: CustomChunkExecutor | None = None,
    ) -> None:
        if index_version < 1:
            raise ValueError("index_version must be positive")
        if max_input_tokens is not None and (
            isinstance(max_input_tokens, bool) or max_input_tokens <= 0
        ):
            raise ValueError("max_input_tokens must be positive")
        self._document_id = document_id
        self._tokenizer = tokenizer
        self._index_version = index_version
        self._max_input_tokens = max_input_tokens
        self._semantic = SemanticBoundaryDetector(semantic) if semantic is not None else None
        if custom is not None and not custom.identity:
            raise ValueError("custom executor identity is required")
        self._custom = custom

    async def chunk(self, doc: ParsedDocument, cfg: ChunkConfig) -> list[ChunkSpec]:
        """Chunk off the event loop, preserving text and exact per-block citations."""
        if cfg.strategy == "semantic" and self._semantic is None:
            raise ChunkUnsupportedStrategy()
        if cfg.strategy == "custom" and (
            self._custom is None
            or cfg.function_version is None
            or cfg.function_version <= 0
            or cfg.function_id is None
        ):
            raise ChunkUnsupportedStrategy()
        if cfg.strategy not in {"semantic", "custom"} and cfg.strategy not in _STRATEGIES:
            raise ChunkUnsupportedStrategy()
        started = perf_counter()
        try:
            copied = cfg.model_copy(deep=True)
            if cfg.strategy == "semantic":
                result = await self._chunk_semantic(doc, copied)
            elif cfg.strategy == "custom":
                result = await self._chunk_custom(doc, copied)
            else:
                result = await to_thread.run_sync(self._chunk, doc, copied)
        except Exception:
            chunk_results.labels(cfg.strategy, "error").inc()
            raise
        finally:
            chunk_duration.labels(cfg.strategy).observe(perf_counter() - started)
        chunk_results.labels(cfg.strategy, "ok").inc()
        log.info("ingestion.chunked", strategy=cfg.strategy, chunks=len(result))
        return result

    def _prefix(self, section: _Section, cfg: ChunkConfig) -> str:
        if cfg.prepend_heading_path and section.heading_path:
            return " > ".join(section.heading_path) + "\n\n"
        return ""

    @staticmethod
    def _normalize_for_embedding(text: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", text).split())

    def _within_model_limit(self, text: str) -> bool:
        if self._max_input_tokens is None:
            return True
        return (
            self._tokenizer.count(text) <= self._max_input_tokens
            and self._tokenizer.count(self._normalize_for_embedding(text)) <= self._max_input_tokens
        )

    def _fits(
        self,
        prefix: str,
        body: str,
        raw_budget: int,
        *,
        model_bounded: bool,
    ) -> bool:
        content = prefix + body
        return self._tokenizer.count(content) <= raw_budget and (
            not model_bounded
            or (self._within_model_limit(content) and self._within_model_limit(body))
        )

    def _bounded_prefix(
        self,
        section: _Section,
        cfg: ChunkConfig,
        body: str,
        raw_budget: int,
        *,
        model_bounded: bool,
    ) -> str:
        prefix = self._prefix(section, cfg)
        if (
            prefix
            and self._max_input_tokens is not None
            and not self._fits(
                prefix,
                body,
                raw_budget,
                model_bounded=model_bounded,
            )
        ):
            return ""
        return prefix

    def _content_budget(self, section: _Section, target: int, *, model_bounded: bool) -> int:
        if not model_bounded or self._max_input_tokens is None:
            return target
        if section.atomic:
            return self._max_input_tokens
        return min(target, self._max_input_tokens)

    def _fit(
        self,
        window: _Window,
        start: int,
        prefix: str,
        budget: int,
        *,
        model_bounded: bool,
    ) -> int:
        probe = min(budget, window.end - start)
        while True:
            body = window.section.text[start : start + probe]
            combined = prefix + body
            candidate_length = probe
            if self._tokenizer.count(combined) > budget:
                try:
                    candidate = self._tokenizer.truncate(combined, budget)
                except ValueError as exc:
                    raise ChunkBudgetExceeded() from exc
                if not combined.startswith(candidate):
                    raise ChunkBudgetExceeded(
                        "The tokenizer did not return an original-text prefix."
                    )
                length = len(candidate) - len(prefix)
                if length <= 0:
                    raise ChunkBudgetExceeded()
                candidate_length = length
            if not self._fits(
                prefix,
                window.section.text[start : start + candidate_length],
                budget,
                model_bounded=model_bounded,
            ):
                low = 1
                high = candidate_length - 1
                if not self._fits(
                    prefix,
                    window.section.text[start : start + low],
                    budget,
                    model_bounded=model_bounded,
                ):
                    raise ChunkBudgetExceeded()
                while low < high:
                    middle = (low + high + 1) // 2
                    if self._fits(
                        prefix,
                        window.section.text[start : start + middle],
                        budget,
                        model_bounded=model_bounded,
                    ):
                        low = middle
                    else:
                        high = middle - 1
                return start + low
            if candidate_length < probe:
                return start + candidate_length
            if start + probe == window.end:
                return window.end
            probe = min(probe * 2, window.end - start)

    def _boundary(
        self,
        text: str,
        prefix: str,
        budget: int,
        cfg: ChunkConfig,
        *,
        model_bounded: bool,
    ) -> int:
        if cfg.strategy == "fixed":
            return len(text)
        separators = tuple(dict.fromkeys((*cfg.separators, *_SENTENCE_SEPARATORS)))
        for separator in separators:
            if not separator:
                continue
            position = text.rfind(separator)
            if position < 0:
                continue
            end = position if separator.startswith("\n#") else position + len(separator)
            if (
                end > 0
                and cfg.min_chunk_tokens <= self._tokenizer.count(prefix + text[:end])
                and self._fits(
                    prefix,
                    text[:end],
                    budget,
                    model_bounded=model_bounded,
                )
            ):
                return end
        return len(text)

    def _overlap_start(self, text: str, limit: int) -> int:
        if not limit:
            return len(text)
        low, high = 0, len(text)
        while low < high:
            middle = (low + high) // 2
            if self._tokenizer.count(text[middle:]) <= limit:
                high = middle
            else:
                low = middle + 1
        return low

    def _windows(
        self,
        window: _Window,
        cfg: ChunkConfig,
        budget: int,
        overlap: int,
        *,
        model_bounded: bool = False,
    ) -> list[_Window]:
        raw_budget = self._content_budget(
            window.section,
            budget,
            model_bounded=model_bounded,
        )
        whole_prefix = self._bounded_prefix(
            window.section,
            cfg,
            window.body,
            raw_budget,
            model_bounded=model_bounded,
        )
        if window.section.atomic and (
            not model_bounded
            or self._fits(
                whole_prefix,
                window.body,
                raw_budget,
                model_bounded=True,
            )
        ):
            return [window]
        result: list[_Window] = []
        start = window.start
        previous_end = start
        while start < window.end:
            prefix = self._bounded_prefix(
                window.section,
                cfg,
                window.section.text[start : start + 1],
                raw_budget,
                model_bounded=model_bounded,
            )
            end = self._fit(
                window,
                start,
                prefix,
                raw_budget,
                model_bounded=model_bounded,
            )
            if end < window.end:
                end = start + self._boundary(
                    window.section.text[start:end],
                    prefix,
                    raw_budget,
                    cfg,
                    model_bounded=model_bounded,
                )
            if end <= previous_end:
                start = previous_end
                prefix = self._bounded_prefix(
                    window.section,
                    cfg,
                    window.section.text[start : start + 1],
                    raw_budget,
                    model_bounded=model_bounded,
                )
                end = self._fit(
                    window,
                    start,
                    prefix,
                    raw_budget,
                    model_bounded=model_bounded,
                )
                if end < window.end:
                    end = start + self._boundary(
                        window.section.text[start:end],
                        prefix,
                        raw_budget,
                        cfg,
                        model_bounded=model_bounded,
                    )
            candidate = _Window(window.section, start, end)
            if candidate.sources:
                result.append(candidate)
            if end == window.end:
                break
            previous_end = end
            start = start + self._overlap_start(window.section.text[start:end], overlap)
            if start == candidate.start:
                start = end
        return result

    def _chunk(self, doc: ParsedDocument, cfg: ChunkConfig) -> list[ChunkSpec]:
        result: list[ChunkSpec] = []
        config_key = self._config_key(cfg)
        for section in _sections(doc, cfg):
            whole = _Window(section, 0, len(section.text))
            if cfg.strategy == "parent_child":
                for parent in self._windows(whole, cfg, cfg.parent_tokens, 0):
                    spec = self._spec(
                        parent,
                        cfg,
                        config_key,
                        len(result),
                        "parent",
                        None,
                        cfg.parent_tokens,
                        doc.language,
                    )
                    result.append(spec)
                    for child in self._windows(
                        parent,
                        cfg,
                        cfg.child_tokens,
                        cfg.child_overlap,
                        model_bounded=self._max_input_tokens is not None,
                    ):
                        result.append(
                            self._spec(
                                child,
                                cfg,
                                config_key,
                                len(result),
                                "child",
                                spec.id,
                                cfg.child_tokens,
                                doc.language,
                            )
                        )
            else:
                for window in self._windows(
                    whole,
                    cfg,
                    cfg.child_tokens,
                    cfg.child_overlap,
                    model_bounded=self._max_input_tokens is not None,
                ):
                    result.append(
                        self._spec(
                            window,
                            cfg,
                            config_key,
                            len(result),
                            "standalone",
                            None,
                            cfg.child_tokens,
                            doc.language,
                        )
                    )
        return result

    async def _chunk_custom(self, doc: ParsedDocument, cfg: ChunkConfig) -> list[ChunkSpec]:
        assert self._custom is not None
        assert cfg.function_id is not None
        assert cfg.function_version is not None
        ref = FunctionVersionRef(cfg.function_id, cfg.function_version)
        executor_identity = f"{self._custom.identity}:{ref.function_id}:{ref.version}"
        config_key = self._config_key(cfg, executor_identity)
        scope = CustomChunkScope(
            document_id=self._document_id,
            index_version=self._index_version,
            tokenizer_fingerprint=self._tokenizer.fingerprint,
            config_key=config_key,
        )
        limits = CustomChunkLimits()
        validation_doc = deepcopy(doc)
        execution_doc = deepcopy(validation_doc)
        validation_cfg = cfg.model_copy(deep=True)
        execution_cfg = validation_cfg.model_copy(deep=True)
        chunks = await self._custom.execute(ref, execution_doc, execution_cfg, scope, limits)
        validated = validate_custom_chunks(
            chunks,
            doc=validation_doc,
            cfg=validation_cfg,
            tokenizer=self._tokenizer,
            scope=scope,
            limits=limits,
        )
        for chunk in validated:
            if chunk.metadata["embed"] and not self._within_model_limit(chunk.content):
                raise ChunkBudgetExceeded("Custom chunk output exceeds the model token limit.")
        return validated

    def _config_key(self, cfg: ChunkConfig, extension_identity: str | None = None) -> str:
        return sha256(
            json.dumps(
                {
                    "version": 1,
                    "config": cfg.model_dump(mode="json"),
                    "tokenizer": self._tokenizer.fingerprint,
                    "index": self._index_version,
                    **({"extension": extension_identity} if extension_identity is not None else {}),
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    async def _chunk_semantic(self, doc: ParsedDocument, cfg: ChunkConfig) -> list[ChunkSpec]:
        assert self._semantic is not None
        result: list[ChunkSpec] = []
        config_key = self._config_key(cfg, self._semantic.identity)
        for section in _sections(doc, cfg):
            whole = _Window(section, 0, len(section.text))
            if section.atomic:
                windows = self._windows(
                    whole,
                    cfg,
                    cfg.child_tokens,
                    cfg.child_overlap,
                    model_bounded=self._max_input_tokens is not None,
                )
            else:
                units = self._semantic_units(whole, cfg)
                semantic_inputs = [unit.body for unit in units]
                if self._max_input_tokens is not None and any(
                    not text.strip() or not self._within_model_limit(text)
                    for text in semantic_inputs
                ):
                    raise ChunkBudgetExceeded(
                        "Semantic boundary input exceeds the model token limit."
                    )
                peaks = await self._semantic.boundaries(semantic_inputs)
                windows = self._semantic_windows(units, peaks, cfg)
            for window in windows:
                result.append(
                    self._spec(
                        window,
                        cfg,
                        config_key,
                        len(result),
                        "standalone",
                        None,
                        cfg.child_tokens,
                        doc.language,
                    )
                )
        return result

    def _semantic_units(self, window: _Window, cfg: ChunkConfig) -> list[_Window]:
        text = window.body
        separators = tuple(dict.fromkeys((*cfg.separators, *_SENTENCE_SEPARATORS)))
        boundaries: set[int] = {len(text)}
        for separator in separators:
            if not separator:
                continue
            position = text.find(separator)
            while position >= 0:
                boundaries.add(position + len(separator))
                position = text.find(separator, position + len(separator))
        coalesced: list[int] = []
        for end in sorted(boundaries):
            previous = coalesced[-1] if coalesced else 0
            if not text[previous:end].strip():
                if coalesced:
                    coalesced[-1] = end
                continue
            coalesced.append(end)
        result: list[_Window] = []
        start = window.start
        model_bounded = self._max_input_tokens is not None
        budget = self._content_budget(
            window.section,
            cfg.child_tokens,
            model_bounded=model_bounded,
        )
        for relative_end in coalesced:
            end = window.start + relative_end
            while start < end:
                prefix = self._bounded_prefix(
                    window.section,
                    cfg,
                    window.section.text[start : start + 1],
                    budget,
                    model_bounded=model_bounded,
                )
                fitted = self._fit(
                    window,
                    start,
                    prefix,
                    budget,
                    model_bounded=model_bounded,
                )
                fitted = min(fitted, end)
                if fitted <= start:
                    raise ChunkBudgetExceeded()
                result.append(_Window(window.section, start, fitted))
                start = fitted
        return self._coalesce_blank_semantic_units(result)

    @staticmethod
    def _coalesce_blank_semantic_units(units: list[_Window]) -> list[_Window]:
        result: list[_Window] = []
        leading_start: int | None = None
        for unit in units:
            if unit.body.strip():
                start = leading_start if leading_start is not None else unit.start
                result.append(_Window(unit.section, start, unit.end))
                leading_start = None
            elif result:
                previous = result[-1]
                result[-1] = _Window(previous.section, previous.start, unit.end)
            elif leading_start is None:
                leading_start = unit.start
        return result

    def _semantic_windows(
        self,
        units: list[_Window],
        peaks: frozenset[int],
        cfg: ChunkConfig,
    ) -> list[_Window]:
        if not units:
            return []
        prefix = self._prefix(units[0].section, cfg)
        accepted: list[int] = []
        start = units[0].start
        for boundary in sorted(peaks):
            if boundary <= 0 or boundary >= len(units):
                continue
            end = units[boundary - 1].end
            remaining_end = units[-1].end
            if (
                self._tokenizer.count(prefix + units[0].section.text[start:end])
                >= cfg.min_chunk_tokens
                and self._tokenizer.count(prefix + units[0].section.text[end:remaining_end])
                >= cfg.min_chunk_tokens
            ):
                accepted.append(boundary)
                start = end
        result: list[_Window] = []
        first = 0
        for last in [*accepted, len(units)]:
            semantic_window = _Window(
                units[0].section,
                units[first].start,
                units[last - 1].end,
            )
            result.extend(
                self._windows(
                    semantic_window,
                    cfg,
                    cfg.child_tokens,
                    cfg.child_overlap,
                    model_bounded=self._max_input_tokens is not None,
                )
            )
            first = last
        return result

    def _spec(
        self,
        window: _Window,
        cfg: ChunkConfig,
        config_key: str,
        ordinal: int,
        role: Literal["parent", "child", "standalone"],
        parent_id: UUID | None,
        budget: int,
        language: str,
    ) -> ChunkSpec:
        model_bounded = role != "parent" and self._max_input_tokens is not None
        raw_budget = self._content_budget(
            window.section,
            budget,
            model_bounded=model_bounded,
        )
        prefix = self._bounded_prefix(
            window.section,
            cfg,
            window.body,
            raw_budget,
            model_bounded=model_bounded,
        )
        content = prefix + window.body
        if model_bounded and not self._within_model_limit(content):
            raise ChunkBudgetExceeded("Chunk output exceeds the model token limit.")
        content_hash = sha256(content.encode()).hexdigest()
        citations = [
            {
                "block_ordinal": source.block.ordinal,
                "kind": source.block.kind,
                "start": max(window.start, source.start) - source.start,
                "end": min(window.end, source.end) - source.start,
                "heading_path": list(source.block.heading_path),
                "page": source.block.page,
                "bbox": list(source.block.bbox) if source.block.bbox is not None else None,
                "source_url": source.block.source_url,
            }
            for source in window.sources
        ]
        tokens = self._tokenizer.count(content)
        identity = json.dumps(
            [config_key, ordinal, role, content_hash, citations],
            sort_keys=True,
            ensure_ascii=False,
        )
        return ChunkSpec(
            id=uuid5(self._document_id, identity),
            document_id=self._document_id,
            ordinal=ordinal,
            content=content,
            content_hash=content_hash,
            token_count=tokens,
            parent_id=parent_id,
            metadata={
                "role": role,
                "embed": role != "parent",
                "language": language,
                "heading_path": list(window.section.heading_path),
                "page": citations[0]["page"] if citations else None,
                "source_url": citations[0]["source_url"] if citations else None,
                "ordinal": ordinal,
                "citations": citations,
                "oversized": tokens > budget,
                "oversized_reason": "table" if tokens > budget else None,
            },
        )
