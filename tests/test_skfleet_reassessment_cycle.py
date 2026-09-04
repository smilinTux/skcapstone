"""Focused contracts for the existing five minute reassessment cycle."""

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ROTATE = ROOT / "scripts/fleet/skfleet-rotate.py"


def _functions(*names):
    source = ROTATE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    namespace = {"Path": Path, "json": json, "re": __import__("re")}
    selected = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


def _report(**changes):
    report = {
        "read_only": True,
        "classes": {},
        "counts": {},
        "excluded_card_ids": [],
        "content_sha256": "a" * 64,
    }
    report.update(changes)
    return report


def test_only_authority_host_gets_shared_full_report_path(tmp_path):
    function = _functions("_full_reassessment_path")["_full_reassessment_path"]
    assert function("chiap08", tmp_path) == tmp_path / "lifecycle-reassessment.json"
    for host in ("chiap01", "chiap02", "chiap03", "chiap04"):
        assert function(host, tmp_path) is None


def test_non_authority_summary_is_compact_and_points_to_authority():
    function = _functions("_reassessment_summary")["_reassessment_summary"]
    summary = function("chiap03", _report(counts={"stale_claims": 2}), None)
    assert summary == (
        "REASSESSMENT|chiap03|report=authority:chiap08 sha256="
        + "a" * 64
        + ' counts={"stale_claims":2} excluded=0'
    )
    assert "classes" not in summary


@pytest.mark.parametrize(
    "report",
    [
        None,
        _report(read_only=False),
        _report(classes=None),
        _report(counts=None),
        _report(excluded_card_ids=None),
        _report(content_sha256="not-a-hash"),
    ],
)
def test_incomplete_assessment_fails_closed(report):
    function = _functions("_validate_reassessment")["_validate_reassessment"]
    with pytest.raises(ValueError):
        function(report)


def test_report_size_cap_fails_before_writer(tmp_path):
    function = _functions("_write_bounded_report")["_write_bounded_report"]
    report_path = tmp_path / "report.json"
    with pytest.raises(ValueError, match="exceeds"):
        function({"large": "x" * 100}, report_path, limit=20)
    assert not report_path.exists()


def test_report_size_cap_measures_exact_emitted_bytes(tmp_path):
    function = _functions("_write_bounded_report")["_write_bounded_report"]
    report_path = tmp_path / "report.json"
    report = {"rows": [{"x": value} for value in range(110_000)]}
    compact_size = len(json.dumps(report, sort_keys=True, separators=(",", ":")).encode())
    assert compact_size < 2 * 1024 * 1024

    with pytest.raises(ValueError, match="exceeds"):
        function(report, report_path)

    assert not report_path.exists()


def test_report_writer_emits_only_the_validated_payload(tmp_path):
    function = _functions("_write_bounded_report")["_write_bounded_report"]
    report_path = tmp_path / "report.json"
    report = _report(counts={"stale_claims": 2})

    function(report, report_path)

    expected = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    assert report_path.read_bytes() == expected
    assert report_path.stat().st_size == len(expected) <= 2 * 1024 * 1024


def test_existing_mutations_remain_after_assessment_and_dry_fenced():
    source = ROTATE.read_text(encoding="utf-8")
    assessment = source.index("assessment=_validate_reassessment(")
    dry = source.index("if DRY:", source.index("def reap_dead_claims"))
    reap = source.index("reap_dead_claims()", dry)
    reviews = source.index("open_provisional_reviews(review_capacity", reap)
    claims = source.index('claim=subprocess.run([SKC,"coord","claim"', reviews)
    assert assessment < dry < reap < reviews < claims
    assert 'raise RuntimeError("lifecycle reassessment module unavailable")' in source
