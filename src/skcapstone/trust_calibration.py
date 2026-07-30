"""Re-export shim: trust calibration moved to capauth (kernel track M1).

The implementation now lives in ``capauth.trust.calibration`` (the L0
identity/authz core). This module keeps ``skcapstone.trust_calibration``
importable byte-identically for every existing importer (the CLI, shell,
mcp_server, the trust pillar, the trust MCP tools, and tests). New code should
import from ``capauth.trust`` directly.

The on-disk calibration format (``~/.skcapstone/trust/calibration.json``) and
FEB derivation are unchanged. ``recommend_thresholds`` no longer imports
skcapstone (capauth is the L0 core and must not reach up into this subapp).
Instead it takes a ``feb_provider`` parameter; the skcapstone callers pass
``skcapstone.pillars.trust.list_febs``, so the skcapstone-side behavior is
identical while capauth stays dependency-clean.

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
