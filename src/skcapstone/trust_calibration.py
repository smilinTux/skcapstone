"""Re-export shim: trust calibration moved to capauth (kernel track M1).

The implementation now lives in ``capauth.trust.calibration`` (the L0
identity/authz core). This module keeps ``skcapstone.trust_calibration``
importable byte-identically for every existing importer (the CLI, shell,
mcp_server, the trust pillar, the trust MCP tools, and tests). New code should
import from ``capauth.trust`` directly.

The on-disk calibration format (``~/.skcapstone/trust/calibration.json``) and
FEB derivation are unchanged. ``recommend_thresholds`` still reads FEB summaries
via ``skcapstone.pillars.trust.list_febs`` (a lazy import inside capauth), so
the skcapstone-side behavior is identical.

See the SKWorld platform reconciled design, spine M1.
"""

from __future__ import annotations

from capauth.trust.calibration import (
    CALIBRATION_FILENAME,
    DEFAULT_THRESHOLDS,
    TrustThresholds,
    apply_setting,
    load_calibration,
    recommend_thresholds,
    save_calibration,
)

__all__ = [
    "CALIBRATION_FILENAME",
    "DEFAULT_THRESHOLDS",
    "TrustThresholds",
    "apply_setting",
    "load_calibration",
    "recommend_thresholds",
    "save_calibration",
]
