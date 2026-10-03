"""Synthetic qualification of the design-only assessment artifact checks."""

import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "assessment_contract", Path(__file__).with_name("design_assessment_contract.py")
)
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


@pytest.fixture
def report(tmp_path, monkeypatch):
    """Use clearly synthetic source bytes, never claim a completed assessment."""
    path = tmp_path / "src/example.py"
    path.parent.mkdir()
    path.write_text("# synthetic qualification only\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(contract, "SOURCES", {"src/example.py": digest})
    return dict(
        **contract.IDENTITY,
        recommendation="defer",
        summary="Synthetic qualification, not the real design assessment.",
        findings=[
            dict(
                kind="existing",
                statement="Synthetic source exists.",
                citations=[dict(path="src/example.py", sha256=digest, start_line=1, end_line=1)],
            )
        ],
        deployed_observations=[
            dict(status="unverified", statement="Not inspected.", receipt_sha256=None)
        ],
        minimal_changes=["No implementation proposed by this fixture."],
        pilot_acceptance=["Independent reviewer must assess the real recommendation."],
        limitations=["Synthetic fixture only."],
    )


def test_valid_structure_is_not_substantive_acceptance(report, tmp_path):
    """A syntactically valid report passes without pretending to grade its prose."""
    contract.validate_report(report, tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "unknown"),
        ("card", "ffffffff"),
        ("assessed_revision", "0" * 40),
        ("proposal_sha256", "0" * 64),
        ("archive_sha256", "0" * 64),
        ("recommendation", "approve-deployment"),
        ("summary", " "),
        ("findings", []),
        ("pilot_acceptance", []),
        ("minimal_changes", []),
        ("limitations", []),
        ("deployed_observations", []),
    ],
)
def test_invalid_contract_denied(report, tmp_path, field, value):
    """Missing evidence or an authority-changing recommendation is rejected."""
    report[field] = value
    with pytest.raises(AssertionError):
        contract.validate_report(report, tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "../secret"),
        ("sha256", "0" * 64),
        ("start_line", True),
        ("start_line", 0),
        ("end_line", 2),
    ],
)
def test_invalid_citation_denied(report, tmp_path, field, value):
    """A citation must name exact approved bytes and a real bounded line span."""
    report["findings"][0]["citations"][0][field] = value
    with pytest.raises(AssertionError):
        contract.validate_report(report, tmp_path)


def test_changed_source_and_symlink_denied(report, tmp_path):
    """The report cannot bind rewritten source or redirect a citation."""
    path = tmp_path / "src/example.py"
    path.write_text("changed\n")
    with pytest.raises(AssertionError):
        contract.validate_report(report, tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(AssertionError):
        contract.validate_report(report, tmp_path)


def test_assumptions_and_live_observations_remain_distinct(report, tmp_path):
    """Unsupported source findings and invented observed receipts fail closed."""
    value = copy.deepcopy(report)
    value["findings"][0]["citations"] = []
    with pytest.raises(AssertionError):
        contract.validate_report(value, tmp_path)
    value = copy.deepcopy(report)
    value["deployed_observations"][0]["status"] = "observed"
    with pytest.raises(AssertionError):
        contract.validate_report(value, tmp_path)


def test_missing_oversize_or_redirected_report_denied(tmp_path):
    """The explicit report check never skips unavailable or redirected input."""
    with pytest.raises(AssertionError):
        contract.regular_bytes(tmp_path, "report.json", 8)
    (tmp_path / "report.json").write_text("x" * 9)
    with pytest.raises(AssertionError):
        contract.regular_bytes(tmp_path, "report.json", 8)
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AssertionError):
        contract.regular_bytes(tmp_path, "alias/report.json", 16)
