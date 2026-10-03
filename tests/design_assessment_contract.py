"""Explicit native recipe for card 888b4895, not a substantive design verdict.

The non-test filename keeps the unfinished assessment out of ordinary repo CI.
The operator profile explicitly invokes this file and requires its one test.
"""

import hashlib
import json
import re
from pathlib import Path

REPORT = "docs/assessments/888b4895.json"
IDENTITY = {
    "schema": "skfleet.design-assessment/v1",
    "card": "888b4895",
    "assessed_revision": "51a3f5dd3093c8e4a388760eed7ca223f6e22f67",
    "proposal_sha256": ("c31fb37031b3535c93d2c9020014c61bd16138fd97a2016806570366507ea137"),
    "archive_sha256": ("e894e39d342fc8fe6e9320ceef93df0cd9b8c3303212bfbafbb6e09dd4d14df2"),
}
SOURCES = {
    "src/skcapstone/context_loader.py": (
        "2977630870f2592b0d87ac5cf7233da468f16a461aec616998b6eb3c0e5bfb77"
    ),
    "src/skcapstone/seat_runtime.py": (
        "5a066cb87a4922f99852b8f17872725019f331d0205d49cfd1679a577e83bdaa"
    ),
    "src/skcapstone/fleet/builder_dispatch.py": (
        "a6d8b00f717b464cc4f6e36137e8fdaba73941626c2bc2eaa6e7d3fcc4fe6080"
    ),
    "src/skcapstone/fleet/workspace_lifecycle.py": (
        "1c1185b946b77f9713e889746709ef037c369c6074aff7c49e472829bf30861d"
    ),
    "src/skcapstone/fleet/worker_watchdog.py": (
        "049c72fbd2c96ed0cd105c3f9532fd42a974f1f6d3241bccbaec79458842e080"
    ),
    "src/skcapstone/fleet/churn_breaker.py": (
        "5c77c80916956794bc093431bf9724d66383812b1d3e0164a0f081e272e5c553"
    ),
    "scripts/fleet/pi-cardstore-guard.mjs": (
        "da17f3c32a5d17be6b9967b4ea694d10f0381158a0617852f72884bfbc6d9feb"
    ),
    "src/skcapstone/data/lifecycle-seat-profiles.json": (
        "3b0bfa281239e5b9fcb0b88ad22527e3eac6293ead102a7228ff5a68296232c6"
    ),
    "src/skcapstone/cli/coord.py": (
        "23907466111ea4bb8b9bb5815d77e00572127e4f151b4595ef3d91ba7f6eda0d"
    ),
}


def text(value):
    """Require bounded narrative without judging its substantive correctness."""
    assert isinstance(value, str) and value.strip() and len(value) <= 8000


def regular_bytes(root, relative, limit):
    """Read only a bounded regular file without following any symlink component."""
    path = root
    for part in Path(relative).parts:
        assert part not in {"..", ".", "/"}
        path = path / part
        assert not path.is_symlink()
    assert path.is_file() and path.stat().st_size <= limit
    return path.read_bytes()


def validate_report(value, root):
    """Check the exact assessment artifact, leaving reasoning quality to review."""
    fields = set(IDENTITY) | {
        "recommendation",
        "summary",
        "findings",
        "deployed_observations",
        "minimal_changes",
        "pilot_acceptance",
        "limitations",
    }
    assert isinstance(value, dict) and set(value) == fields
    assert all(value[key] == expected for key, expected in IDENTITY.items())
    assert value["recommendation"] in {"accept", "revise", "defer"}
    text(value["summary"])
    for section in ["minimal_changes", "pilot_acceptance", "limitations"]:
        rows = value[section]
        assert isinstance(rows, list) and 1 <= len(rows) <= 32
        for row in rows:
            text(row)
    findings = value["findings"]
    assert isinstance(findings, list) and 1 <= len(findings) <= 32
    cited = 0
    for finding in findings:
        assert isinstance(finding, dict) and set(finding) == {"kind", "statement", "citations"}
        assert finding["kind"] in {"existing", "gap", "assumption"}
        text(finding["statement"])
        citations = finding["citations"]
        assert isinstance(citations, list) and len(citations) <= 16
        assert citations or finding["kind"] == "assumption"
        for citation in citations:
            assert isinstance(citation, dict) and set(citation) == {
                "path",
                "sha256",
                "start_line",
                "end_line",
            }
            assert citation["path"] in SOURCES
            raw = regular_bytes(root, citation["path"], 2 * 1024 * 1024)
            assert (
                hashlib.sha256(raw).hexdigest() == SOURCES[citation["path"]] == citation["sha256"]
            )
            start, end = citation["start_line"], citation["end_line"]
            assert type(start) is int and type(end) is int
            assert 1 <= start <= end <= len(raw.splitlines())
            cited += 1
    assert cited, "An assessment needs at least one pinned source citation"
    observations = value["deployed_observations"]
    assert isinstance(observations, list) and 1 <= len(observations) <= 32
    for row in observations:
        assert isinstance(row, dict) and set(row) == {"status", "statement", "receipt_sha256"}
        assert row["status"] in {"observed", "unverified"}
        text(row["statement"])
        digest = row["receipt_sha256"]
        if row["status"] == "observed":
            assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        else:
            assert digest is None


def test_exact_design_assessment_report():
    """Missing or malformed final report fails the explicit native recipe."""
    root = Path(__file__).resolve().parents[1]
    raw = regular_bytes(root, REPORT, 128 * 1024)
    validate_report(json.loads(raw), root)
