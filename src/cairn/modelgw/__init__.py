"""Model gateway.

The registry — providers, models, envelope-encrypted credentials and their
Redis projections. Embedding calls are made by ``cairn.embedding`` through the
provider families registered here (TEI, Infinity, bge-m3, DashScope, Volcengine
Ark, OpenAI-compatible). General chat invocation (streaming, fallback chains,
usage accounting) is the remaining M10 work.

Pulled forward because ``M03`` cannot validate a knowledge base's embedding
configuration without somewhere to look models up, and a stub would have to be
torn out again. What exists here is the durable half; nothing about the
invocation layer is prejudged.
"""

from cairn.modelgw.catalog import ModelCatalog, get_model_catalog
from cairn.modelgw.dto import ModelRef, ModelView, ProviderView

__all__ = ["ModelCatalog", "ModelRef", "ModelView", "ProviderView", "get_model_catalog"]
