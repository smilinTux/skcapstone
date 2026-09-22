"""Shared wall-clock budgets for the serialized Seraph seat cycle."""

from __future__ import annotations

from .rotation_lock import SERAPH_LOCK_WAIT_SECONDS

GENERATION_ADMISSION_SECONDS = 600
SERAPH_SERVICE_DEADLINE_SECONDS = 300
SERAPH_PARENT_WAIT_SECONDS = 310
SERAPH_PROCESS_GROUP_CLEANUP_SECONDS = 5
SERAPH_RECEIPT_MARGIN_SECONDS = 30
SERAPH_DISPATCH_TIMEOUT_SECONDS = 180

# An override must leave every enclosing boundary strictly larger than the
# lock wait, child runtime, process-group reap, and durable-receipt margin.
SERAPH_DISPATCH_TIMEOUT_LIMIT_SECONDS = (
    min(
        SERAPH_SERVICE_DEADLINE_SECONDS,
        SERAPH_PARENT_WAIT_SECONDS,
        GENERATION_ADMISSION_SECONDS // 2,
    )
    - SERAPH_LOCK_WAIT_SECONDS
    - SERAPH_PROCESS_GROUP_CLEANUP_SECONDS
    - SERAPH_RECEIPT_MARGIN_SECONDS
)

if not SERAPH_DISPATCH_TIMEOUT_SECONDS < SERAPH_DISPATCH_TIMEOUT_LIMIT_SECONDS:
    raise RuntimeError("Seraph dispatcher timeout violates the nested seat-cycle budget")
