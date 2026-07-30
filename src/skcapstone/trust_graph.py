"""Re-export shim: trust-web graph moved to capauth (kernel track M1).

The implementation now lives in ``capauth.trust.graph`` (the L0 identity/authz
core). This module keeps ``skcapstone.trust_graph`` importable byte-identically
for every existing importer (the CLI, shell, dashboard, mcp_server, the trust
MCP tools, and tests). New code should import from ``capauth.trust`` directly.

Re-exporting the public names as module attributes preserves attribute-style
access (``skcapstone.trust_graph.build_trust_graph``) and monkeypatch points
that callers like ``dashboard.py`` rely on.

See the SKWorld platform reconciled design, spine M1.
"""

from __future__ import annotations

from capauth.trust.graph import (
    FORMATTERS,
    TrustEdge,
    TrustGraph,
    TrustNode,
    build_trust_graph,
    format_dot,
    format_json,
    format_table,
)

__all__ = [
    "FORMATTERS",
    "TrustEdge",
    "TrustGraph",
    "TrustNode",
    "build_trust_graph",
    "format_dot",
    "format_json",
    "format_table",
]
