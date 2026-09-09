"""Dense embedding without control-plane imports or database access."""

from cairn.core.modelref import ModelRef
from cairn.embedding.base import Vector
from cairn.embedding.service import EmbeddingService

__all__ = ["EmbeddingService", "ModelRef", "Vector"]
