"""Behaviour tests for the wedged-worker actuator in skfleet-rotate.py.

The source is lifted out of the shipped script with ast (the same pattern as
test_worker_progress_report.py) so these exercise the code that runs on the
fleet rather than a paraphrase of it.  Nothing here touches a live seat: the
systemd stop, the release CLI and the fold read are all injected.

Contract 1 of docs/fleet/2026-09-18-learnings.md is why the call site is
asserted and not only the function.
"""

from __future__ import annotations

import ast
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet.worker_watchdog import (
    DEFAULT_WEDGE_TIMEOUT_S,
    WEDGE_ACTUATING_STATES,
    ProgressObservation,
    wedge_actuation_fenced,
)

SRC = Path(__file__).resolve().parent.parent / "scripts" / "fleet" / "skfleet-rotate.py"

CARD = "139ec63d"
OWNER = "pi-glm-chiap03-139ec63d"
REVISION = "revision-1"
UNIT = "skfleet-worker-glm-139ec63d.service"
LIFT = (
    "_reap_wedged_workers",
    "_wedge_mode",
    "_stop_wedged_unit",
    "_record_wedge_outcome",
    "_append_reaper_evidence",
    "_claim_ttl_release_cmd",
    "_WEDGE_WRITER",
)


def _tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert found, f"{name} is not defined at module level in {SRC.name}"
    return found[0]


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                names.add(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                names.add(child.func.attr)
    return names


class Harness:
    """One synthetic host: a claim store, a unit, and a log."""

    def __init__(self, tmp_path: Path):
        self.lines: list[str] = []
        self.evidence_dir = tmp_path / "card_events"
        self.claimed = True
        self.owner = OWNER
        self.revision = REVISION
        self.claim_ts = time.time() - 6 * 3600 - 18 * 60
        self.commands: list[list[str]] = []
        self.unit_active = True
        self.release_rc = 0
        self.release_frees = True

        tree = _tree()
        body = [
            node
            for node in tree.body
            if (isinstance(node, ast.FunctionDef) and node.name in LIFT)
            or (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in LIFT)
        ]
        lifted = {n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in body}
        assert not set(LIFT) - lifted, f"missing from {SRC.name}: {set(LIFT) - lifted}"
        self.ns: dict[str, object] = {
            "os": os,
            "json": json,
            "time": time,
            "re": re,
            "hashlib": hashlib,
            "fcntl": fcntl,
            "datetime": datetime,
            "subprocess": subprocess,
            "d": None,
            "log": lambda _d, message: self.lines.append(message),
            "HOST": "chiap03",
            "DRY": False,
            "SKC": "/nonexistent/skcapstone",
            "DISPATCH_AGENT": "test-dispatcher",
            "_EVID_DIR": str(self.evidence_dir),
            "_WORKER_UNIT_RE": re.compile(
                r"skfleet-worker-(codex|glm|qwen|kimi|escalate)-([0-9a-f]{8})\.service"
            ),
            "WEDGE_ACTUATING_STATES": WEDGE_ACTUATING_STATES,
            "DEFAULT_WEDGE_TIMEOUT_S": DEFAULT_WEDGE_TIMEOUT_S,
            "wedge_actuation_fenced": wedge_actuation_fenced,
            "_current_claim_identity_fresh": self.fresh_identity,
            "_claim_ttl_fresh_state": self.fold_state,
        }
        exec(compile(ast.Module(body=body, type_ignores=[]), str(SRC), "exec"), self.ns)

    # --- injected seams ----------------------------------------------------
    def fresh_identity(self, _cid):
        if not self.claimed:
            return (None, None, None)
        return (self.owner, self.claim_ts, self.revision)

    def fold_state(self, _cid):
        return "claimed" if self.claimed else "released"

    def runner(self, cmd):
        self.commands.append(list(cmd))
        if cmd[:3] == ["systemctl", "--user", "stop"]:
            self.unit_active = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return SimpleNamespace(returncode=0 if self.unit_active else 3)
        if "release-claim" in cmd:
            if self.release_rc == 0 and self.release_frees:
                self.claimed = False
            return SimpleNamespace(returncode=self.release_rc, stdout="", stderr="no")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def run(self, records=None, env=None, **kwargs):
        return self.ns["_reap_wedged_workers"](
            self.records() if records is None else records,
            runner=self.runner,
            env={"SKFLEET_WEDGE_MODE": "enforce"} if env is None else env,
            now=time.time(),
            **kwargs,
        )

    def records(self, **changes):
        record = {
            "card": CARD,
            "session": "glm-auto-139ec63d",
            "unit": UNIT,
            "owner": OWNER,
            "claim_revision": REVISION,
            "expected_claim_revision": REVISION,
            "state": "progress-stale",
            "wedge": "wedge-stale-confirmed",
            "observation": ProgressObservation(
                owner=OWNER,
                card_id=CARD,
                session_id="glm-auto-139ec63d",
                claim_revision=REVISION,
                expected_claim_revision=REVISION,
                progress_at=datetime.datetime.fromtimestamp(
                    self.claim_ts, datetime.timezone.utc
                ).isoformat(),
                session_alive=True,
            ),
            "progress_at": self.claim_ts,
            "progress_age_s": "22680",
            "claim_age_s": 6 * 3600 + 18 * 60,
            "claim_ts": self.claim_ts,
            "receipt_local": True,
            "truncated": False,
        }
        record.update(changes)
        return [record]

    def logged(self, prefix):
        return [line for line in self.lines if line.startswith(prefix)]

    def evidence(self):
        path = self.evidence_dir / (str(self.ns["_WEDGE_WRITER"]) + ".jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture()
def harness(tmp_path):
    return Harness(tmp_path)


# --- the default is off ------------------------------------------------------


@pytest.mark.parametrize("env", [{}, {"SKFLEET_WEDGE_MODE": ""}, {"SKFLEET_WEDGE_MODE": "yes"}])
def test_unset_or_unrecognised_mode_does_nothing_at_all(harness, env):
    """An unconfigured host must not stop, release, or even read anything."""
    assert harness.run(env=env) == 0
    assert harness.commands == []
    assert harness.claimed is True
    assert harness.evidence() == []


def test_report_mode_names_the_candidate_and_releases_nothing(harness):
    assert harness.run(env={"SKFLEET_WEDGE_MODE": "report"}) == 0
    would = harness.logged("WEDGE_WOULD_STOP|")
    assert len(would) == 1
    assert CARD in would[0] and "verdict=wedge-stale-confirmed" in would[0]
    assert harness.commands == []
    assert harness.claimed is True
    assert harness.evidence() == []


def test_enforce_without_go_is_a_dry_run(harness):
    """DRY gates every board mutation, not only the launch."""
    assert harness.run(dry=True) == 0
    assert harness.commands == []
    assert harness.claimed is True
    assert "dry_run" in harness.logged("WEDGE|")[0]


# --- the happy path ----------------------------------------------------------


def test_enforce_stops_records_and_releases_under_a_cas(harness):
    assert harness.run() == 1
    assert harness.claimed is False
    stops = [c for c in harness.commands if c[:3] == ["systemctl", "--user", "stop"]]
    assert stops == [["systemctl", "--user", "stop", UNIT]]
    release = [c for c in harness.commands if "release-claim" in c][0]
    # The CAS is the whole fence: a worker that re-claimed since the scan
    # causes a refusal rather than a theft.
    assert "--expected-claim-revision" in release
    assert release[release.index("--expected-claim-revision") + 1] == REVISION
    assert release[release.index("--owner") + 1] == OWNER
    # The stop must precede the release: a worker that is still running must
    # not have its card handed to someone else.
    assert harness.commands.index(stops[0]) < harness.commands.index(release)


def test_the_reason_and_its_evidence_are_recorded_on_the_card(harness):
    harness.run()
    rows = harness.evidence()
    assert [row["action"] for row in rows] == ["verdict", "link"]
    assert all(row["card_id"] == CARD for row in rows)
    assert rows[0]["verdict"] == "WORKER_WEDGED"
    detail = rows[1]["link_value"]
    for expected in (
        "verdict=wedge-stale-confirmed",
        "progress_age_s=22680",
        "last_write_at=",
        "claim_age_s=22680",
        "owner=" + OWNER,
        "claim_revision=" + REVISION,
    ):
        assert expected in detail, detail


def test_recording_is_idempotent_across_repeated_ticks(harness, tmp_path):
    harness.run()
    before = harness.evidence()
    harness.claimed = True
    harness.unit_active = True
    harness.run()
    assert harness.evidence() == before


# --- everything that must refuse ---------------------------------------------


def test_a_reclaim_between_the_scan_and_the_act_refuses(harness):
    """A newer generation must never be released on an older observation."""
    harness.revision = "revision-2"
    assert harness.run() == 0
    assert harness.commands == []
    assert harness.claimed is True
    assert harness.logged("WEDGE_RECLAIMED|")


def test_a_release_by_someone_else_refuses(harness):
    harness.claimed = False
    assert harness.run() == 0
    assert harness.commands == []


def test_a_failed_stop_leaves_the_claim_held_and_writes_no_verdict(harness):
    """A stop that fails must not leave WORKER_WEDGED on a running worker."""

    def stopper(cmd):
        harness.commands.append(list(cmd))
        return SimpleNamespace(returncode=0)  # is-active keeps returning 0

    assert harness.run(stopper=stopper) == 0
    assert harness.claimed is True
    assert harness.evidence() == []
    assert harness.logged("WEDGE_STOP_FAILED|")


def test_an_unrecognised_unit_name_never_reaches_systemctl(harness):
    """The record is assembled from systemd output and card ids."""
    assert harness.run(records=harness.records(unit="evil; rm -rf /")) == 0
    assert harness.commands == []
    assert harness.logged("WEDGE_STOP_FAILED|")


def test_a_worker_with_no_unit_is_never_stopped(harness):
    """A migration-era tmux worker has no unit to stop, so this path skips it."""
    assert harness.run(records=harness.records(unit="")) == 0
    assert harness.claimed is True


@pytest.mark.parametrize(
    "verdict",
    [
        "wedge-progressing",
        "wedge-within-margin",
        "wedge-unmeasured",
        "wedge-refused-progress-exited",
        "wedge-refused-progress-claim-mismatch",
    ],
)
def test_no_other_verdict_is_ever_a_candidate(harness, verdict):
    assert harness.run(records=harness.records(wedge=verdict)) == 0
    assert harness.commands == []
    assert harness.claimed is True


def test_the_fence_is_re_evaluated_against_the_fresh_claim(harness):
    """A record whose verdict says wedged but whose observation no longer
    supports it is refused, so a stale or hand-edited record cannot act."""
    fresh = harness.records()[0]
    fresh["observation"] = ProgressObservation(
        owner=OWNER,
        card_id=CARD,
        session_id="glm-auto-139ec63d",
        claim_revision=REVISION,
        expected_claim_revision=REVISION,
        progress_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        session_alive=True,
    )
    assert harness.run(records=[fresh]) == 0
    assert harness.commands == []
    assert harness.logged("WEDGE_FENCE_REFUSED|")


def test_a_release_that_reports_success_but_changes_nothing_is_not_counted(harness):
    """The two stores can disagree; a zero exit is not proof the claim moved."""
    harness.release_frees = False
    assert harness.run() == 0
    assert harness.logged("WEDGE_INEFFECTIVE|")


def test_a_failed_release_is_not_counted(harness):
    harness.release_rc = 1
    assert harness.run() == 0
    assert harness.logged("WEDGE_RELEASE_FAILED|")


def test_a_wrapper_that_released_on_stop_counts_and_is_not_released_twice(harness):
    """Stopping the unit runs the wrapper's own exact-generation release.

    The sanctioned owner returning its own claim is the best outcome, not a
    failure of this path, and it must not be followed by a second release.
    """

    def stopper(cmd):
        harness.commands.append(list(cmd))
        if cmd[:3] == ["systemctl", "--user", "stop"]:
            harness.unit_active = False
            harness.claimed = False
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=3)

    assert harness.run(stopper=stopper) == 1
    assert not [c for c in harness.commands if "release-claim" in c]
    assert harness.logged("WEDGE_RELEASED_BY_WRAPPER|")


# --- the call site -----------------------------------------------------------


def test_the_actuator_is_called_with_the_reporters_records():
    """Contract 1: assert the call site, not only the function."""
    health = _function(_tree(), "reap_dead_claims")
    calls = [
        node
        for node in ast.walk(health)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_reap_wedged_workers"
    ]
    assert calls, "reap_dead_claims does not call _reap_wedged_workers"
    assert "_report_worker_progress" in _called_names(calls[0])


def test_the_reporter_still_does_not_actuate():
    """Measurement and actuation stay separate functions, deliberately."""
    reporter = _function(_tree(), "_report_worker_progress")
    forbidden = {"run", "Popen", "_stop_wedged_unit", "_record_wedge_outcome"}
    assert not _called_names(reporter) & forbidden


def test_the_pass_cannot_abort_the_rotation_cycle():
    """A watchdog has no business breaking dispatch.

    The progress pass runs between the quorum gate and ``_expire_idle_claims``
    plus the review-and-close phases, all inside one unguarded module-scope
    call.  An exception escaping it would reap nothing AND silently drop the
    rest of the rotation, which is the same trap the eager ``GATED_EXIT_CODE``
    import and the claim TTL store read both sprang before.
    """
    health = _function(_tree(), "reap_dead_claims")
    guarded = [
        node
        for node in ast.walk(health)
        if isinstance(node, ast.Try)
        and "_reap_wedged_workers" in _called_names(ast.Module(body=node.body, type_ignores=[]))
    ]
    assert guarded, "the wedge pass is not wrapped in a try"
    handlers = guarded[0].handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0].type, ast.Name) and handlers[0].type.id == "Exception"


# --- the fold is the only authority ------------------------------------------


def test_the_reason_survives_a_card_store_fold(harness, tmp_path):
    """Re-fold what was written, through CardStore, not through our own replay.

    This is the check that catches the recurring failure in this codebase: a
    writer that appends happily to a store nothing reads back.  Measured here
    on 2026-09-19 and worth stating plainly, because it also applies to the
    absence reaper's WORKER_DIED row: ``_OVERLAY_TO_STORE_ACTION`` maps move,
    priority, swimlane, labels, link, assign, unassign and describe and
    NOTHING else, so an ``action: "verdict"`` row in the card_events overlay
    is silently dropped by the fold.  The link row is what survives, which is
    why the link row is where the evidence lives.
    """
    from skcapstone.card_store import CardStore

    home = tmp_path / ".skcapstone"
    harness.ns["_EVID_DIR"] = str(home / "coordination" / "card_events")
    harness.evidence_dir = home / "coordination" / "card_events"
    harness.run()

    card = home / "cards" / CARD
    (card / "events").mkdir(parents=True, exist_ok=True)
    (card / "core.json").write_text(json.dumps({"id": CARD, "title": "wedge", "status": "DOING"}))
    store = CardStore(home)
    folded = [*store._read_events(CARD), *store._legacy_events(CARD)]

    links = [e for e in folded if e.get("action") == "link"]
    assert len(links) == 1, folded
    assert links[0]["link_key"] == "worker_wedged"
    for expected in ("verdict=wedge-stale-confirmed", "progress_age_s=", "claim_age_s="):
        assert expected in links[0]["link_value"]

    # Pinned deliberately: if a later skcoord release starts folding verdict
    # rows this fails, and that is a change worth noticing rather than a
    # silent improvement.
    assert not [e for e in folded if e.get("action") == "verdict"], (
        "the fold now surfaces overlay verdict rows; the comment in "
        "_record_wedge_outcome and the absence reaper's WORKER_DIED row "
        "both need revisiting"
    )
