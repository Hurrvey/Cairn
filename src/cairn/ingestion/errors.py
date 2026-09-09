"""Stable contributor-facing parsing failures (M07 §11)."""

from cairn.core.errors import CairnError


class ParseError(CairnError):
    code = "PARSE_FAILED"
    http_status = 422
    title = "Document parsing failed"
    retryable = False


class ParseCorruptFile(ParseError):
    code = "PARSE_CORRUPT_FILE"
    title = "The file could not be read. It may be corrupt."


class ParseUnsupportedMime(ParseError):
    code = "PARSE_UNSUPPORTED_MIME"
    http_status = 415
    title = "This file type is not supported."


class ParseEmptyContent(ParseError):
    code = "PARSE_EMPTY_CONTENT"
    title = "No extractable text was found. If this is a scan, enable OCR."


class ParseTooLarge(ParseError):
    code = "PARSE_TOO_LARGE"
    http_status = 413
    title = "The file exceeds the configured parsing size limit."


class ChunkError(CairnError):
    code = "CHUNK_FAILED"
    http_status = 422
    title = "Document chunking failed"
    retryable = False


class ChunkUnsupportedStrategy(ChunkError):
    code = "CHUNK_UNSUPPORTED_STRATEGY"
    title = "The configured chunking strategy is not available."


class ChunkBudgetExceeded(ChunkError):
    code = "CHUNK_BUDGET_EXCEEDED"
    title = "The chunk token budget cannot accommodate the heading and source text."
