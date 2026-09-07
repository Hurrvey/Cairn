"""Model gateway.

**Phase 2 ships only the registry** — providers, models, and encrypted
credentials. The invocation layer (LiteLLM adapters, streaming, circuit
breaking, usage accounting) lands with M10 in Phase 3.

Pulled forward because ``M03`` cannot validate a knowledge base's embedding
configuration without somewhere to look models up, and a stub would have to be
torn out again. What exists here is the durable half; nothing about the
invocation layer is prejudged.
"""

from cairn.modelgw.catalog import ModelCatalog, get_model_catalog
from cairn.modelgw.dto import ModelRef, ModelView, ProviderView

__all__ = ["ModelCatalog", "ModelRef", "ModelView", "ProviderView", "get_model_catalog"]
