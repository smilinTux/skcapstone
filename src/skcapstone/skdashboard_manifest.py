"""skdashboard's SKWorld module manifest (umbrella shell spec 5 + reconciled 4.2).

skdashboard is the coordination/Team-Board surface skcapstone already serves on
:7778 (``skcapstone.dashboard``). Folding it into the umbrella shell makes it a
first-class SKWorld subapp: like skchat/skcode/skos it declares ONE capauth-signed
skworld.module.json with two facets. The UI facet lets the shell mount the Board
as a shell section; the operator facet lets Atlas watch and steer the dashboard.

This module builds the manifest as a pure dict from the serving origin, so the
served URLs are origin-relative (they resolve against wherever the dashboard
actually answers, avoiding host/port drift). The manifest is public discovery
metadata (no secrets) and is served unauthenticated at
/.well-known/skworld-module.json from the dashboard's own web server (mirroring
skchat's webui.py and skcode's daemon.py).

UI facet: Grade B, the web-embed path the umbrella spec assigns skdashboard
(spec 4.4 names skdashboard as the canonical Grade B web-embed example). The entry
points at the dashboard's served root; a grade promotion to a native Flutter
module is then a manifest edit plus a package, never a contract change (reconciled
spec 2.3).

nav.order 40 slots the Board AFTER Code (30) and ahead of the operator/ops area,
matching the shell section order (OS=10, Chats=20, Code=30, Board=40).

The operator block mirrors operator_seat/skdashboard_adapter.py. Unlike the other
subapps both this manifest and its adapter live in the SAME repo (skcapstone), so
there is no cross-repo drift risk; the manifest-adapter drift-guard test
(tests/operator_seat/test_manifest_adapter_conformance.py) still asserts
manifest.operator.conditions == skdashboard_adapter.CONDITIONS exactly, keeping the
two locked together.
"""

from __future__ import annotations

#: The manifest schema version (sk-standards manifest schema v1.1, +operator block).
SCHEMA_VERSION = "1.1"
#: The audience skdashboard tokens are minted for. Mirrors the capauth
#: AUDIENCE_SCOPES convention (a minimal read-only ``<app>.read`` scope until the
#: dashboard needs a wider verb set); tighten/expand there when it does.
AUDIENCE = "skdashboard"


def skdashboard_module_manifest(base_url: str) -> dict:
    """Build skdashboard's skworld.module.json for a given serving origin.

    Args:
        base_url: The origin the dashboard answers on (e.g. the request base URL,
            "http://127.0.0.1:7778/"). URLs in the manifest are built relative to
            this so they never hardcode a host or port.

    Returns:
        The manifest dict (UI facet + operator facet).
    """
    base = base_url.rstrip("/")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "skdashboard",
        "name": "Board",
        # UI facet: Grade B (the web-embed path per umbrella spec 4.4). Promotes
        # to Grade A by flipping grade + adding entry.flutter_package, never a
        # contract change (reconciled spec 2.3).
        "grade": "B",
        "entry": {"url": f"{base}/"},
        # nav.order 40 slots the Board after Code (30) and before the ops area:
        # OS=10, Chats=20, Code=30, Board=40.
        "nav": {"icon": "dashboard", "order": 40, "label": "Board"},
        "deeplinkPrefix": "skworld://skdashboard/",
        "auth": {
            "audience": AUDIENCE,
            "scopes": ["skdashboard.read"],
        },
        "memory": {"opt_in": True, "scope": "skdashboard"},
        "health": f"{base}/api/status",
        # Operator facet: what Atlas's skdashboard adapter observes and may act on.
        "operator": {
            "contractVersion": 1,
            "cli": "skcapstone dashboard operator",
            "repos": ["skcapstone"],
            "conditions": [
                "DashboardReady",
                "BoardReadable",
            ],
            "proposedStandardActions": ["restart-dashboard"],
        },
    }


__all__ = ["skdashboard_module_manifest", "SCHEMA_VERSION", "AUDIENCE"]
