"""Call-site and behaviour tests for ``coord slice-preflight``.

Contract 1 of docs/fleet/2026-09-18-learnings.md: card_slicing was written,
tested, and called by nothing. These tests assert the entry point exists and
reaches the real recommender, recommendation-only, with the parent's
CompositionVerificationContract in the report.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _invoke(tmp_path: Path, *arguments: str):
    return CliRunner().invoke(
        _main(),
        ["coord", "slice-preflight", *arguments, "--home", str(tmp_path)],
        catch_exceptions=False,
    )


def test_slice_preflight_command_is_registered(tmp_path: Path):
    """The recommender must be reachable from the coord CLI at all."""
    result = _invoke(tmp_path, "deadbeef")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["card_id"] == "deadbeef"
    assert report["known"] is False


def test_bounded_card_is_reported_bounded(tmp_path: Path):
    CardStore(tmp_path).create(
        CardCore(
            id="aaaa0001",
            title="One small thing",
            acceptance_criteria=["it works"],
        )
    )
    result = _invoke(tmp_path, "aaaa0001")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["known"] is True
    assert report["decision"] == "bounded"
    assert report["actuation"] == "recommendation-only"


def test_oversized_card_reports_leaves_and_composition_contract(tmp_path: Path):
    CardStore(tmp_path).create(
        CardCore(
            id="bbbb0002",
            title="A sprawling epic",
            acceptance_criteria=[f"criterion-{i}" for i in range(6)],
            meta={
                "repository": "https://github.com/example/project",
                "base_ref": "main",
                "deliverables": [f"deliverable-{i}" for i in range(6)],
                "verification_surfaces": [f"tests/surface_{i}.py" for i in range(6)],
                "focused_gates": [f"pytest -q tests/surface_{i}.py" for i in range(6)],
                "mutation_boundaries": [f"src/part_{i}.py" for i in range(6)],
            },
        )
    )
    result = _invoke(tmp_path, "bbbb0002")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["decision"] == "reject"
    assert report["leaves"], "an oversized card must come back with leaf recommendations"
    contract = report["composition_verification"]
    assert contract is not None
    assert contract["coverage_sha256"]
    assert contract["leaf_ids"]
    assert contract["checks"]


def test_active_card_is_advisory_never_split(tmp_path: Path):
    """A claimed card must never come back with an actionable split."""
    CardStore(tmp_path).create(
        CardCore(
            id="cccc0003",
            title="Owned sprawling work",
            initial_owner="pi-codex-host-cccc0003",
            initial_claim_revision="rev-1",
            acceptance_criteria=[f"criterion-{i}" for i in range(6)],
            meta={
                "repository": "https://github.com/example/project",
                "base_ref": "main",
                "deliverables": [f"deliverable-{i}" for i in range(6)],
                "verification_surfaces": [f"tests/surface_{i}.py" for i in range(6)],
                "focused_gates": [f"pytest -q tests/surface_{i}.py" for i in range(6)],
            },
        )
    )
    result = _invoke(tmp_path, "cccc0003")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["decision"] == "advisory"
    assert report["leaves"] == []
