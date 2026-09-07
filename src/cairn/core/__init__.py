"""Core foundation — primitives with zero domain knowledge.

If anything in this package starts importing a domain concept (knowledge bases,
documents, retrieval), the architecture has failed. Enforced by the
``core_is_domain_free`` contract in ``.importlinter``.
"""
