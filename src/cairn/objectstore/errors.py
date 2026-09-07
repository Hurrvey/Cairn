"""Object store errors."""

from __future__ import annotations

from cairn.core.errors import NotFound, UpstreamUnavailable, ValidationFailed

__all__ = ["ObjectNotFound", "ObjectStoreUnavailable", "ObjectTooLarge"]


class ObjectNotFound(NotFound):
    code = "OBJECT_NOT_FOUND"
    title = "Object not found"


class ObjectStoreUnavailable(UpstreamUnavailable):
    code = "STORAGE_BINDING_UNAVAILABLE"
    title = "Object store unavailable"


class ObjectTooLarge(ValidationFailed):
    code = "OBJECT_TOO_LARGE"
    title = "Object exceeds the permitted size"
