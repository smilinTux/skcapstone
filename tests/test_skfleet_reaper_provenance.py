"""Regression tests for exact fleet launch provenance in the fast reaper."""

from __future__ import annotations

import ast
import collections
import datetime
import glob
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _claim_event(
    owner: str,
    claim_revision: str,
    timestamp: str | None,
) -> dict[str, object]:
    """Return one full-schema CardStore claim event."""
    event: dict[str, object] = {
        "event_id": f"claim-{claim_revision}",
        "writer": owner,
        "node": "chiap02",
        "seq": 0,
        "action": "claim",
        "owner": owner,
        "claim_revision": claim_revision,
        "transition_id": f"transition-{claim_revision}",
    }
    if timestamp is not None:
        event["ts"] = timestamp
    return event


def _load_functions(*names: str) -> dict[str, object]:
    """Load selected functions without executing the fleet launcher."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(wanted) == set(names)
    module = ast.Module(body=[wanted[name] for name in names], type_ignores=[])
    namespace: dict[str, object] = {
        "collections": collections,
        "datetime": datetime,
        "glob": glob,
        "json": json,
        "os": os,
        "ROTATION_HOSTS": ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08"),
    }
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def _reaper_fixture(
    tmp_path: Path,
    *,
    card_id: str,
    owner: str,
    claim_revision: str | None,
    launch_revision: str | None,
    launch_lines: list[str] | None = None,
) -> tuple[dict[str, object], list[list[str]], list[str]]:
    """Build one isolated claimed card and a fake release command."""
    cards = tmp_path / "cards"
    card = cards / card_id
    events = card / "events"
    events.mkdir(parents=True)
    core = {
        "id": card_id,
        "kind": "task",
        "title": "Full-schema reaper fixture",
        "description": "Synthetic task used only by the isolated test.",
        "created_by": "pytest",
        "created_at": "2026-08-28T18:00:00+00:00",
        "acceptance_criteria": [],
        "dependencies": [],
        "initial_priority": "high",
        "initial_swimlane": "feature",
        "initial_labels": ["source-only"],
        "meta": {},
    }
    (card / "core.json").write_text(json.dumps(core) + "\n", encoding="utf-8")
    claim = _claim_event(
        owner,
        claim_revision or "missing-revision",
        datetime.datetime.fromtimestamp(time.time() - 900, tz=datetime.timezone.utc).isoformat(),
    )
    if claim_revision is not None:
        claim["claim_revision"] = claim_revision
    else:
        claim.pop("claim_revision")
    (events / "claim.jsonl").write_text(json.dumps(claim) + "\n", encoding="utf-8")

    live = tmp_path / "live"
    live.mkdir()
    for host in ("chiap01", "chiap02", "chiap03"):
        (live / f"{host}.json").write_text("{}\n", encoding="utf-8")

    evidence = tmp_path / "evidence"
    actions = evidence / "20260828T180000Z" / "actions.log"
    actions.parent.mkdir(parents=True)
    if launch_lines is None:
        launch_lines = []
        if launch_revision is not None:
            launch_lines.append(
                f"LAUNCHED|chiap02|codex-auto-{card_id}|{card_id}|lane=codex"
                f"|model=sk-codex|owner={owner}|claim_revision={launch_revision}"
            )
    actions.write_text(
        "".join(f"{line}\n" for line in launch_lines),
        encoding="utf-8",
    )

    namespace = _load_functions(
        "_claim_identity",
        "_current_claim",
        "_acts_fresh_rows",
        "_current_claim_identity_fresh",
        "_fleet_launch_provenance",
        "reap_dead_claims",
    )
    released: list[list[str]] = []
    messages: list[str] = []

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        """Record the release command and report success."""
        released.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    namespace.update(
        {
            "CARDS": str(cards),
            "CLAIM_GRACE": 300,
            "EVID": str(evidence),
            "HOST": "chiap08",
            "KNOWN_HOST_TTL": 86400,
            "LIVE": str(live),
            "REAP_QUORUM": 3,
            "SKC": "skcapstone",
            "_EPHEMERAL_OWNER": re.compile(r"^(pi|codex|glm)[-_]"),
            "_fleet_launch_claims": None,
            "_load_ineffective": lambda: set(),
            "_record_ineffective": lambda _card: None,
            "_rows": {},
            "d": str(tmp_path / "run"),
            "event_rows": namespace["_acts_fresh_rows"],
            "lifecycle_state": lambda _card: "open" if released else "claimed",
            "live_report": lambda: (time.time(), set(), 3),
            "log": lambda _directory, message: messages.append(message),
            "subprocess": SimpleNamespace(run=fake_run),
            "time": time,
        }
    )
    return namespace, released, messages


def _replace_fresh_claim(
    tmp_path: Path,
    *,
    owner: str,
    claim_revision: str,
    timestamp: str | None,
) -> None:
    """Replace the on-disk event seen only by the fresh CardStore read."""
    event = _claim_event(owner, claim_revision, timestamp)
    path = tmp_path / "cards" / "deadbeef" / "events" / "claim.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def test_manual_claim_with_no_exact_launch_generation_is_preserved(tmp_path: Path) -> None:
    """The 93220ffc manual Codex failure mode never reaches release-claim."""
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="93220ffc",
        owner="codex-chiap08-93220ffc",
        claim_revision="bffb51e374a74854b2dd0a070b9f363c",
        launch_revision="an-older-claim-generation",
    )

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_UNPROVEN|") for message in messages)


def test_missing_claim_revision_is_never_replaced_by_event_id(tmp_path: Path) -> None:
    """A legacy claim event ID is not an exact claim revision."""
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="93220ffc",
        owner="codex-chiap08-93220ffc",
        claim_revision=None,
        launch_revision="claim-event",
    )

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any("claim revision missing" in message for message in messages)


@pytest.mark.parametrize(
    "launch_lines",
    [
        [
            "LAUNCHED|chiap02|codex-auto-deadbeef|deadbeef|"
            "owner=pi-codex-chiap02-deadbeef|claim_revision=revision-1"
        ],
        [
            "LAUNCHED|chiap02|codex-auto-deadbeef|deadbeef|lane=codex|"
            "model=sk-codex|owner=wrong|owner=pi-codex-chiap02-deadbeef|"
            "claim_revision=wrong|claim_revision=revision-1"
        ],
        [
            "LAUNCHED|unknown|codex-auto-deadbeef|deadbeef|lane=codex|"
            "model=sk-codex|owner=pi-codex-unknown-deadbeef|"
            "claim_revision=revision-1"
        ],
        [
            "LAUNCHED|chiap02|wrong-session|deadbeef|lane=codex|"
            "model=sk-codex|owner=pi-codex-chiap02-deadbeef|"
            "claim_revision=revision-1"
        ],
        [
            "LAUNCHED|chiap02|codex-auto-deadbeef|deadbeef|lane=codex|"
            "model=sk-codex|owner=pi-codex-chiap03-deadbeef|"
            "claim_revision=revision-1"
        ],
        [
            "LAUNCHED|chiap02|codex-auto-deadbeef|deadbeef|lane=unknown|"
            "model=sk-codex|owner=pi-unknown-chiap02-deadbeef|"
            "claim_revision=revision-1"
        ],
        [
            "LAUNCHED|chiap02|codex-auto-deadbeef|deadbeef|lane=codex|"
            "model=sk-codex|owner=pi-codex-chiap02-deadbeef|"
            "claim_revision=revision-1",
            "LAUNCHED|chiap02|codex-auto-deadbeef|deadbeef|lane=codex|"
            "model=sk-codex|owner=pi-codex-chiap02-deadbeef|"
            "claim_revision=revision-1",
        ],
    ],
    ids=(
        "minimal",
        "duplicate-fields",
        "unknown-host",
        "wrong-session",
        "wrong-owner",
        "unknown-lane",
        "duplicate-records",
    ),
)
def test_malformed_or_duplicate_launch_evidence_fails_closed(
    tmp_path: Path, launch_lines: list[str]
) -> None:
    """Only one exact launcher-schema record proves fleet provenance."""
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner="pi-codex-chiap02-deadbeef",
        claim_revision="revision-1",
        launch_revision=None,
        launch_lines=launch_lines,
    )

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_UNPROVEN|") for message in messages)


def test_successful_launch_records_exact_claim_generation() -> None:
    """Only a successful launch renders owner and revision provenance."""
    fields = _load_functions("_launch_claim_fields")["_launch_claim_fields"]
    assert fields("pi-codex-chiap02-deadbeef", "revision-1", True) == (
        "|owner=pi-codex-chiap02-deadbeef|claim_revision=revision-1"
    )
    assert fields("pi-codex-chiap02-deadbeef", "revision-1", False) == ""
    assert fields("pi-codex-chiap02-deadbeef", "", True) == ""


def test_every_fleet_release_call_supplies_expected_revision() -> None:
    """The reaper, worker trap, and launch-failure path all use the fence."""
    source = ROTATE.read_text(encoding="utf-8")
    assert source.count("--expected-claim-revision") == 3


def test_genuine_dead_fleet_claim_with_exact_generation_is_released(
    tmp_path: Path,
) -> None:
    """A dead fleet worker remains releasable when exact provenance matches."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "fleet-claim-revision"
    namespace, released, _messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )

    assert namespace["reap_dead_claims"]() == 1
    assert released == [
        [
            "skcapstone",
            "coord",
            "release-claim",
            "deadbeef",
            "--owner",
            owner,
            "--expected-claim-revision",
            revision,
            "--agent",
            "fleet-liveness-reaper",
        ]
    ]


def test_quorum_and_fresh_owner_fences_still_fail_closed(tmp_path: Path) -> None:
    """The provenance check does not weaken existing distributed fences."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "fleet-claim-revision"
    namespace, released, _messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )
    namespace["live_report"] = lambda: (time.time(), set(), 2)
    assert namespace["reap_dead_claims"]() == 0
    assert released == []

    namespace["live_report"] = lambda: (time.time(), set(), 3)
    namespace["_load_ineffective"] = lambda: {"deadbeef"}
    assert namespace["reap_dead_claims"]() == 0
    assert released == []

    namespace["_load_ineffective"] = lambda: set()
    namespace["live_report"] = lambda: (time.time(), {"deadbeef"}, 3)
    assert namespace["reap_dead_claims"]() == 0
    assert released == []

    namespace["live_report"] = lambda: (time.time(), set(), 3)
    recent_event = _claim_event(
        owner,
        revision,
        datetime.datetime.fromtimestamp(time.time(), tz=datetime.timezone.utc).isoformat(),
    )
    namespace["event_rows"] = lambda _card: [recent_event]
    assert namespace["reap_dead_claims"]() == 0
    assert released == []

    old_event = _claim_event(
        owner,
        revision,
        datetime.datetime.fromtimestamp(time.time() - 900, tz=datetime.timezone.utc).isoformat(),
    )
    namespace["event_rows"] = lambda _card: [old_event]
    namespace["_current_claim_identity_fresh"] = lambda _card: (
        "pi-codex-chiap03-deadbeef",
        time.time() - 900,
        "replacement-revision",
    )
    assert namespace["reap_dead_claims"]() == 0
    assert released == []


def test_newer_same_owner_recent_claim_gets_its_own_grace(tmp_path: Path) -> None:
    """A fresh same-owner generation cannot inherit the cached generation's age."""
    owner = "pi-codex-chiap02-deadbeef"
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision="old-revision",
        launch_revision="new-revision",
    )
    old_event = _claim_event(
        owner,
        "old-revision",
        datetime.datetime.fromtimestamp(time.time() - 900, tz=datetime.timezone.utc).isoformat(),
    )
    namespace["event_rows"] = lambda _card: [old_event]
    _replace_fresh_claim(
        tmp_path,
        owner=owner,
        claim_revision="new-revision",
        timestamp=datetime.datetime.fromtimestamp(
            time.time() - 30, tz=datetime.timezone.utc
        ).isoformat(),
    )

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_RECLAIMED|") for message in messages)


def test_newer_same_owner_old_claim_is_still_a_different_generation(
    tmp_path: Path,
) -> None:
    """A newer revision is preserved even when its own timestamp is old."""
    owner = "pi-codex-chiap02-deadbeef"
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision="old-revision",
        launch_revision="new-revision",
    )
    old_event = _claim_event(
        owner,
        "old-revision",
        datetime.datetime.fromtimestamp(time.time() - 1200, tz=datetime.timezone.utc).isoformat(),
    )
    namespace["event_rows"] = lambda _card: [old_event]
    _replace_fresh_claim(
        tmp_path,
        owner=owner,
        claim_revision="new-revision",
        timestamp=datetime.datetime.fromtimestamp(
            time.time() - 900, tz=datetime.timezone.utc
        ).isoformat(),
    )

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_RECLAIMED|") for message in messages)


@pytest.mark.parametrize(
    ("boundary_offset", "expected_releases"),
    [(-0.001, 0), (0.0, 1), (0.001, 1)],
    ids=("just-before", "at-boundary", "after-boundary"),
)
def test_fresh_same_generation_uses_exact_grace_boundary(
    tmp_path: Path,
    boundary_offset: float,
    expected_releases: int,
) -> None:
    """The fresh timestamp is releasable exactly at and after grace."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "same-revision"
    namespace, released, _messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )
    reference = float(int(time.time()))
    cached_event = _claim_event(
        owner,
        revision,
        datetime.datetime.fromtimestamp(reference - 900, tz=datetime.timezone.utc).isoformat(),
    )
    namespace["event_rows"] = lambda _card: [cached_event]
    fresh_ts = reference - namespace["CLAIM_GRACE"]
    _replace_fresh_claim(
        tmp_path,
        owner=owner,
        claim_revision=revision,
        timestamp=datetime.datetime.fromtimestamp(fresh_ts, tz=datetime.timezone.utc).isoformat(),
    )
    namespace["live_report"] = lambda: (
        fresh_ts + namespace["CLAIM_GRACE"] + boundary_offset,
        set(),
        3,
    )

    assert namespace["reap_dead_claims"]() == expected_releases
    assert len(released) == expected_releases


@pytest.mark.parametrize(
    "timestamp",
    [None, "not-a-timestamp", "2026-08-28T20:00:00"],
    ids=("missing", "malformed", "timezone-ambiguous"),
)
def test_missing_malformed_or_ambiguous_fresh_timestamp_fails_closed(
    tmp_path: Path,
    timestamp: str | None,
) -> None:
    """Only an unambiguous aware fresh claim timestamp can authorize release."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "same-revision"
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )
    cached_event = _claim_event(
        owner,
        revision,
        datetime.datetime.fromtimestamp(time.time() - 900, tz=datetime.timezone.utc).isoformat(),
    )
    namespace["event_rows"] = lambda _card: [cached_event]
    _replace_fresh_claim(
        tmp_path,
        owner=owner,
        claim_revision=revision,
        timestamp=timestamp,
    )

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any("claim timestamp invalid" in message for message in messages)


def test_future_fresh_timestamp_fails_closed_on_clock_skew(tmp_path: Path) -> None:
    """A future claim is never aged by a future liveness report."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "same-revision"
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )
    reference = time.time()
    future_ts = reference + 60
    cached_event = _claim_event(
        owner,
        revision,
        datetime.datetime.fromtimestamp(reference - 900, tz=datetime.timezone.utc).isoformat(),
    )
    namespace["event_rows"] = lambda _card: [cached_event]
    _replace_fresh_claim(
        tmp_path,
        owner=owner,
        claim_revision=revision,
        timestamp=datetime.datetime.fromtimestamp(future_ts, tz=datetime.timezone.utc).isoformat(),
    )
    namespace["live_report"] = lambda: (
        future_ts + namespace["CLAIM_GRACE"],
        set(),
        3,
    )

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_CLOCK_SKEW|") for message in messages)
