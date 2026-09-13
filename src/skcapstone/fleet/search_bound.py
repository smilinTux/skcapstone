"""Host-neutral bounded search policy for generated fleet worker briefs.

Card f96b1fd2 [SKFLEET-WORKER-SEARCH-BOUNDING-RSI].

Live reviewer card c2d84daf (worker pi-seraph-chiap08-c2d84daf) launched
``find /home`` and then ``find /`` while reviewing; the child scans ran for
more than 27 minutes until an operator terminated only the child scan
processes. Evidence SHA-256
2d8828c3c2ee1c0a8a2b6bc9a9d4cfe4f6ec2743dc52f0fa440913c7389763b4
(~/.skcapstone/evidence/work/c2d84daf/UNBOUNDED-SEARCH-LIVE-EVIDENCE.md).

The reviewed PR661 six-file worker-brief candidate validates evidence bytes
under exact producer paths but does not constrain search commands, and
``PI_NATIVE_TOOLS`` still includes ``find``. This module is a separate,
host-neutral source repair: it supplies the fail-closed search policy text
that generated worker instructions must carry, plus a fail-closed validator
so a brief missing the policy is rejected rather than shipped.
"""

from __future__ import annotations

#: Exact SHA-256 of the c2d84daf live evidence, verified before this repair.
LIVE_EVIDENCE_SHA256 = (
    "2d8828c3c2ee1c0a8a2b6bc9a9d4cfe4f6ec2743dc52f0fa440913c7389763b4"
)

#: Directive anchors the fail-closed validator requires in every brief.
#: These are stable, load-bearing phrases; do not reword them freely.
REQUIRED_DIRECTIVES = (
    "EXACT AUTHORIZED START PATH",
    "rg --files",
    "BOUNDED FILTERS",
    "TIMEOUTS",
    "find /",
    "find /home",
)


def search_policy_block() -> str:
    """Return the bounded search policy block for generated worker briefs.

    The returned text is spliced verbatim into the fleet worker brief by
    scripts/fleet/skfleet-rotate.py. It is guidance for the worker AND the
    contract that :func:`enforce_search_policy` checks.
    """
    return (
        "\n"
        "SEARCH POLICY (fail-closed, non-negotiable):\n"
        "Live incident: reviewer c2d84daf ran `find /home` then `find /`; child\n"
        "scans ran past 27 minutes until only the child processes were\n"
        "terminated. Evidence SHA-256 " + LIVE_EVIDENCE_SHA256 + ".\n"
        "- Start every repository and evidence search from an\n"
        "  EXACT AUTHORIZED START PATH: the card workspace, the repository\n"
        "  checkout, or the evidence directory named in the card. Never start\n"
        "  a search from / or from a directory the card does not name.\n"
        "- Prefer rg for text discovery and `rg --files` for file discovery.\n"
        "  They are faster and respect .gitignore by default; find is the\n"
        "  fallback, not the default.\n"
        "- Every search needs BOUNDED FILTERS plus an explicit result LIMIT and a\n"
        "  hard TIMEOUTS bound. Kill a search that exceeds its limit or timeout\n"
        "  instead of widening it.\n"
        "- FORBIDDEN: `find /`, `find /home`, and any equivalent broad\n"
        "  filesystem-root or home-root traversal (ls -R, du, or grep -r from\n"
        "  / or /home, or find with no name/type/path filter). If a broad scan\n"
        "  seems unavoidable, run it only under the exact authorized path and\n"
        "  say in your verdict why the bounded alternatives were insufficient.\n"
    )


def enforce_search_policy(brief: str) -> bool:
    """Fail-closed check: does this generated brief bound worker searches?

    Returns True only if the brief carries the full bounded-search policy:
    an exact authorized start path, rg / `rg --files` preference, bounded
    filters with limits and timeouts, and an explicit ban on ``find /`` and
    ``find /home`` (or equivalent broad traversal).

    A brief that merely permits unbounded root/home searches -- the state
    before card f96b1fd2 -- returns False.
    """
    return all(directive in brief for directive in REQUIRED_DIRECTIVES)
