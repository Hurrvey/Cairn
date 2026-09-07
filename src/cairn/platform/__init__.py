"""Platform services — audit, settings, quotas, usage.

Phase 0 ships the ``workspace`` and ``audit_log`` tables plus ``AuditService.record``,
because FR-O-01 requires the bootstrap and every credential change to be audited
from the first release. Query, export, retention, quotas, and usage accounting
land with M15 in Phase 1.

``audit_log`` is created **already partitioned** in the baseline migration: a
plain table cannot be converted to a partitioned one in place, so getting this
wrong now would cost a full-table rewrite later.
"""

from cairn.platform.audit import AuditService, get_audit_service

__all__ = ["AuditService", "get_audit_service"]
