"""Stable retrieval errors."""

from cairn.core.errors import CairnError, UpstreamUnavailable, ValidationFailed


class KbRuntimeUnavailable(CairnError):
    code = "KB_RUNTIME_UNAVAILABLE"
    http_status = 503
    title = "Knowledge base runtime unavailable"


class KbRuntimeCorrupt(KbRuntimeUnavailable):
    code = "KB_RUNTIME_INVALID"
    title = "Knowledge base runtime invalid"


class RetrievalReadModelInvalid(UpstreamUnavailable):
    code = "RETRIEVAL_READ_MODEL_INVALID"
    title = "Retrieval read model invalid"


class RetrievalUnsupported(ValidationFailed):
    code = "RETRIEVAL_OPTION_UNSUPPORTED"
    title = "Retrieval option unsupported"
