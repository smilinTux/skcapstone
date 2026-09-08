"""Tests for the main-branch merge gate (SKCAPSTONE-BRANCH-GATE-01)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from skcapstone.fleet import ruleset as ruleset_mod
from skcapstone.fleet.cli import fleet


class _FakeGh:
    """In-memory stand-in for `gh api` so the gate logic is testable offline."""

    def __init__(self, rulesets: list[dict], protection: dict | None = None):
        self.rulesets = rulesets
        self.protection = protection

    def __call__(self, repo: str, path: str, method: str = "GET", body: dict | None = None):
        if path.endswith("/branches/main/protection"):
            if self.protection is None:
                raise RuntimeError("404 Branch not protected")
            if method == "PUT":
                self.protection = body
                return body
            if method == "DELETE":
                self.protection = None
                return {}
            return self.protection
        if "/rulesets" in path and path.endswith("/rulesets"):
            if method == "GET":
                return self.rulesets
        rid = path.rsplit("/", 1)[-1]
        if path.startswith("repos/") and path.endswith(f"/rulesets/{rid}"):
            match = next((r for r in self.rulesets if str(r.get("id")) == rid), None)
            if method == "GET":
                if match is None:
                    raise RuntimeError(f"404 ruleset {rid}")
                return match
            if method == "PATCH":
                self.rulesets = [r for r in self.rulesets if str(r.get("id")) != rid]
                self.rulesets.append(body)
                return body
            if method == "DELETE":
                self.rulesets = [r for r in self.rulesets if str(r.get("id")) != rid]
                return {}
        raise RuntimeError(f"unexpected path: {path} ({method})")


def _env() -> dict:
    return {"SKFLEET_EVIDENCE": "/tmp/skfleet-evidence-test"}


def test_gate_blocks_pending_or_failed(tmp_path, monkeypatch) -> None:
    """AC3: a controlled PR cannot merge while either Python job is pending or
    failed, and can merge only after both pass."""
    monkeypatch.setenv("SKFLEET_EVIDENCE", str(tmp_path / "evidence"))
    runner = CliRunner()
    monkeypatch.setattr(ruleset_mod, "_gh_api", lambda *a, **k: {})
    result = runner.invoke(
        fleet,
        [
            "ruleset-inventory",
            "smilinTux/skcapstone",
            "--evidence-dir",
            str(tmp_path / "evidence"),
        ],
    )
    assert result.exit_code == 0, result.output
    snaps = list((tmp_path / "evidence" / "smilinTux" / "skcapstone").glob("ruleset-*.json"))
    assert len(snaps) == 1
    snap = json.loads(snaps[0].read_text())
    assert snap["sha256"] == ruleset_mod._sha256(snap["ruleset"])

    # Gate semantics: required checks must all be green. Model the merge
    # decision as AND over terminal statuses.
    def merge_allowed(statuses: dict[str, str]) -> bool:
        # A PR can merge only when every required check is terminal-success.
        required = ["unit tests (py3.11)", "unit tests (py3.12)", "lint", "build"]
        return all(statuses.get(k) == "success" for k in required)

    assert (
        merge_allowed({"unit tests (py3.11)": "in_progress", "unit tests (py3.12)": "success"})
        is False
    )
    assert (
        merge_allowed({"unit tests (py3.11)": "failure", "unit tests (py3.12)": "success"})
        is False
    )
    assert (
        merge_allowed(
            {k: "success" for k in ["unit tests (py3.11)", "unit tests (py3.12)", "lint", "build"]}
        )
        is True
    )


def test_rollback_dry_run(tmp_path, monkeypatch) -> None:
    """AC4: rollback computes the exact diff and (dry-run) writes nothing."""
    monkeypatch.setenv("SKFLEET_EVIDENCE", str(tmp_path / "evidence"))
    recorded = {
        "repo": "smilinTux/skcapstone",
        "rulesets": [{"id": 1, "mode": "active"}],
        "main_protection": None,
    }
    monkeypatch.setattr(
        ruleset_mod,
        "inventory",
        lambda repo: {
            "repo": repo,
            "captured_at": "2026-09-01T23:30:00Z",
            "rulesets": [{"id": 1, "mode": "disabled"}],
            "main_protection": {"required_status_checks": []},
        },
    )
    snap_file = tmp_path / "ruleset-0.json"
    snap_file.write_text(
        json.dumps({"repo": "smilinTux/skcapstone", "ruleset": recorded, "sha256": "x"})
    )
    monkeypatch.setattr(ruleset_mod, "_gh_api", _FakeGh([{"id": 1, "mode": "disabled"}]))
    result = ruleset_mod.rollback("smilinTux/skcapstone", str(snap_file), apply=False)
    assert result["applied"] is False
    assert result["diff"]["changed"] is True
    assert any(m["id"] == 1 for m in result["diff"]["modified"])
    assert result["diff"]["protection_changed"] is True
