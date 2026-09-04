"""Regression tests for exact fleet launch provenance in the fast reaper."""

from __future__ import annotations

import ast
import collections
import datetime
import fcntl
import glob
import hashlib
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
        "fcntl": fcntl,
        "glob": glob,
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "re": re,
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
        "_log_once_per_hour",
        "_parse_worker_owner",
        "_claim_identity",
        "_current_claim",
        "_acts_fresh_rows",
        "_current_claim_identity_fresh",
        "_fleet_launch_provenance",
        "_ineffective_suppresses",
        "_record_reap_outcome",
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
            "BoundaryError": RuntimeError,
            "CARDS": str(cards),
            "CLAIM_GRACE": 300,
            "EVID": str(evidence),
            "HOST": "chiap08",
            "HOME": str(tmp_path),
            "_EVID_DIR": str(tmp_path / "card_events"),
            "KNOWN_HOST_TTL": 86400,
            "LIVE": str(live),
            "REAP_QUORUM": 3,
            "SKC": "skcapstone",
            "_SEAT_RE": re.compile(r"^[a-z][a-z0-9-]{0,31}$"),
            "_fleet_launch_claims": None,
            "REAP_RUNTIME_VERSION": "0.1.63",
            "_load_ineffective": lambda: [],
            "_record_ineffective": lambda *_args: None,
            "_remove_ineffective": lambda *_args: None,
            "_rows": {},
            "d": str(tmp_path / "run"),
            "event_rows": namespace["_acts_fresh_rows"],
            "lifecycle_state": lambda _card: "open" if released else "claimed",
            "live_report": lambda: (time.time(), set(), 3),
            "log": lambda _directory, message: messages.append(message),
            "MeroObservation": lambda **_kwargs: SimpleNamespace(append=lambda _home: {}),
            "Path": Path,
            "_worker_health_snapshot": lambda _sessions: {
                "sessions": 0,
                "claims_exact": 0,
                "mismatched": 0,
                "duplicates": 0,
            },
            "sh": lambda *_args: "",
            "seat_for": lambda card_id, _core: "link" if card_id == "deadbeef" else None,
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


def _ineffective_functions(tmp_path: Path) -> dict[str, object]:
    """Load the isolated ineffective-generation store."""
    namespace = _load_functions(
        "_load_ineffective",
        "_write_ineffective",
        "_ineffective_suppresses",
        "_record_ineffective",
        "_remove_ineffective",
    )
    namespace.update(
        {
            "_INEFFECTIVE_PATH": str(tmp_path / "reap-ineffective.json"),
            "REAP_RUNTIME_VERSION": "0.1.63",
        }
    )
    return namespace


def test_legacy_bare_card_quarantine_is_retried_after_upgrade(tmp_path: Path) -> None:
    """A pre-0.1.63 bare card ID cannot suppress an exact modern generation."""
    path = tmp_path / "reap-ineffective.json"
    path.write_text('{"cards":["deadbeef"]}\n', encoding="utf-8")
    namespace = _ineffective_functions(tmp_path)

    entries = namespace["_load_ineffective"]()

    assert entries == []
    assert not namespace["_ineffective_suppresses"](
        entries, "deadbeef", "pi-codex-chiap02-deadbeef", "revision-1"
    )


def test_ineffective_generation_retries_once_per_runtime_or_claim(tmp_path: Path) -> None:
    """Only an unchanged claim generation on an unchanged runtime stays suppressed."""
    namespace = _ineffective_functions(tmp_path)
    record = namespace["_record_ineffective"]
    suppresses = namespace["_ineffective_suppresses"]
    load = namespace["_load_ineffective"]
    owner = "pi-codex-chiap02-deadbeef"

    record("deadbeef", owner, "revision-1", "release_command_failed")
    record("deadbeef", owner, "revision-1", "release_command_failed")
    entries = load()

    assert len(entries) == 1
    assert entries[0].keys() == {
        "card_id",
        "owner",
        "claim_revision",
        "failure_class",
        "runtime_version",
        "timestamp",
    }
    assert suppresses(entries, "deadbeef", owner, "revision-1")
    assert not suppresses(entries, "deadbeef", owner, "revision-2")

    namespace["REAP_RUNTIME_VERSION"] = "0.1.64"
    assert not suppresses(entries, "deadbeef", owner, "revision-1")
    record("deadbeef", owner, "revision-1", "release_reported_success_noop")
    assert len(load()) == 2
    assert suppresses(load(), "deadbeef", owner, "revision-1")


def test_success_removes_only_the_exact_ineffective_generation(tmp_path: Path) -> None:
    """Resolving one generation cannot erase another claim's quarantine."""
    namespace = _ineffective_functions(tmp_path)
    record = namespace["_record_ineffective"]
    owner = "pi-codex-chiap02-deadbeef"
    record("deadbeef", owner, "revision-1", "release_command_failed")
    record("deadbeef", owner, "revision-2", "release_command_failed")

    namespace["_remove_ineffective"]("deadbeef", owner, "revision-2")

    entries = namespace["_load_ineffective"]()
    assert [(entry["card_id"], entry["claim_revision"]) for entry in entries] == [
        ("deadbeef", "revision-1")
    ]


def test_reaper_retries_exact_0_1_58_failure_once_on_0_1_63(tmp_path: Path) -> None:
    """An exact older-runtime failure does not suppress the upgraded reaper."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "fleet-claim-revision"
    namespace, released, _messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )
    old_failure = {
        "card_id": "deadbeef",
        "owner": owner,
        "claim_revision": revision,
        "failure_class": "release_command_failed",
        "runtime_version": "0.1.58",
        "timestamp": "2026-08-28T18:00:00+00:00",
    }
    namespace["_load_ineffective"] = lambda: [old_failure]

    assert namespace["reap_dead_claims"]() == 1
    assert len(released) == 1

    released.clear()
    current_failure = {**old_failure, "runtime_version": "0.1.63"}
    namespace["_load_ineffective"] = lambda: [current_failure]
    assert namespace["reap_dead_claims"]() == 0
    assert released == []


def test_manual_ephemeral_claim_with_no_launch_generation_is_reaped(tmp_path: Path) -> None:
    """A dead ephemeral worker no longer falls into a nonexistent stale path."""
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="93220ffc",
        owner="codex-chiap08-93220ffc",
        claim_revision="bffb51e374a74854b2dd0a070b9f363c",
        launch_revision="an-older-claim-generation",
    )

    assert namespace["reap_dead_claims"]() == 1
    assert len(released) == 1
    assert any("provenance=ephemeral" in message for message in messages)
    assert not any("stale-claim path" in message for message in messages)


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
    assert any(
        message.startswith("REAP_EXCLUDED|") and "claim revision missing" in message
        for message in messages
    )


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

    assert namespace["reap_dead_claims"]() == 1
    assert len(released) == 1
    assert any("provenance=ephemeral" in message for message in messages)


@pytest.mark.parametrize("lane", ["codex", "qwen"])
def test_launcher_owner_naming_has_exact_provenance(tmp_path: Path, lane: str) -> None:
    """The launch lookup accepts the fleet's Codex and Qwen owner names."""
    owner = f"pi-{lane}-chiap02-deadbeef"
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision="revision-1",
        launch_revision=None,
        launch_lines=[
            f"LAUNCHED|chiap02|{lane}-auto-deadbeef|deadbeef|lane={lane}|"
            f"model=test-model|owner={owner}|claim_revision=revision-1"
        ],
    )

    assert namespace["reap_dead_claims"]() == 1
    assert len(released) == 1
    assert any("provenance=fleet" in message for message in messages)


def test_dead_link_seat_claim_is_reaped_with_exact_provenance(tmp_path: Path) -> None:
    """A dead provisioned seat worker follows the same fenced reap path."""
    owner = "pi-link-chiap02-deadbeef"
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision="seat-revision",
        launch_revision=None,
        launch_lines=[
            "LAUNCHED|chiap02|codex-auto-deadbeef|deadbeef|lane=codex|"
            "model=test-model|owner=pi-link-chiap02-deadbeef|"
            "claim_revision=seat-revision"
        ],
    )

    assert namespace["reap_dead_claims"]() == 1
    assert released[0][5] == owner
    assert any("provenance=fleet" in message for message in messages)


@pytest.mark.parametrize(
    "owner",
    (
        "jarvis-chiap02-deadbeef",
        "link-chiap02-deadbeef",
        "link-unknown-deadbeef",
        "link-chiap02-feedface",
        "pi-unknown-chiap02-deadbeef",
        "pi-codex-chiap02-deadbeef-extra",
    ),
)
def test_worker_owner_parser_rejects_broad_or_mismatched_names(owner: str) -> None:
    """Seat support does not restore the old arbitrary-prefix eligibility."""
    parser = _load_functions("_parse_worker_owner")["_parse_worker_owner"]
    parser.__globals__.update(
        {
            "ROTATION_HOSTS": ("chiap01", "chiap02"),
            "_SEAT_RE": re.compile(r"^[a-z][a-z0-9-]{0,31}$"),
        }
    )
    assert parser(owner, "deadbeef", "link") is None


def test_launch_provenance_resolves_seat_for_each_card(tmp_path: Path) -> None:
    """One seat-owned launch cannot lend its seat to another card."""
    namespace, _released, _messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner="pi-link-chiap02-deadbeef",
        claim_revision="link-revision",
        launch_revision=None,
        launch_lines=[
            "LAUNCHED|chiap02|codex-auto-feedface|feedface|lane=codex|"
            "model=test-model|owner=pi-link-chiap02-feedface|claim_revision=wrong-seat",
            "LAUNCHED|chiap02|codex-auto-deadbeef|deadbeef|lane=codex|"
            "model=test-model|owner=pi-link-chiap02-deadbeef|claim_revision=link-revision",
        ],
    )
    other = tmp_path / "cards" / "feedface"
    other.mkdir()
    (other / "core.json").write_text('{"initial_labels": []}\n', encoding="utf-8")

    assert namespace["_fleet_launch_provenance"](
        "deadbeef", "pi-link-chiap02-deadbeef", "link-revision"
    )
    assert not namespace["_fleet_launch_provenance"](
        "feedface", "pi-link-chiap02-feedface", "wrong-seat"
    )


def test_reap_outcome_is_machine_readable_and_idempotent(tmp_path: Path) -> None:
    """An orphaned claim gets one verdict plus one exact-generation evidence link."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "orphaned-revision"
    namespace, _released, _messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )

    claim_ts = time.time() - 900
    assert namespace["_record_reap_outcome"]("deadbeef", owner, revision, claim_ts)
    assert namespace["_record_reap_outcome"]("deadbeef", owner, revision, claim_ts)
    path = tmp_path / "card_events" / "fleet-liveness-reaper.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    verdict = next(row for row in rows if row["action"] == "verdict")
    evidence = next(row for row in rows if row["action"] == "link")
    assert verdict["verdict"] == "WORKER_DIED"
    assert evidence["link_key"] == "worker_died"
    assert evidence["link_value"] == f"owner={owner} claim_revision={revision}"
    assert len({row["event_id"] for row in rows}) == 2


@pytest.mark.parametrize(
    "mutation",
    ("crlf", "missing-lf", "duplicate", "extra", "malformed", "noncanonical"),
)
def test_preexisting_outcome_requires_exact_canonical_lf_bytes(
    tmp_path: Path, mutation: str
) -> None:
    """Only exact canonical UTF-8 JSONL can make deterministic replay idempotent."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "orphaned-revision"
    namespace, _released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )
    claim_ts = time.time() - 900
    assert namespace["_record_reap_outcome"]("deadbeef", owner, revision, claim_ts)
    path = tmp_path / "card_events" / "fleet-liveness-reaper.jsonl"
    canonical = path.read_bytes()
    first, second = canonical.splitlines(keepends=True)
    mutations = {
        "crlf": canonical.replace(b"\n", b"\r\n"),
        "missing-lf": canonical[:-1],
        "duplicate": canonical + first,
        "extra": canonical + b"extra",
        "malformed": b"not-json\n" + second,
        "noncanonical": json.dumps(json.loads(first), sort_keys=True).encode("utf-8")
        + b"\n"
        + second,
    }
    path.write_bytes(mutations[mutation])

    assert not namespace["_record_reap_outcome"]("deadbeef", owner, revision, claim_ts)
    assert path.read_bytes() == mutations[mutation]
    assert any(message.startswith("REAP_OUTCOME_FAILED|") for message in messages)


@pytest.mark.parametrize("reported_at", ("future", "not-finite"))
def test_invalid_live_report_timestamp_cannot_bypass_claim_grace(
    tmp_path: Path, reported_at: str
) -> None:
    """An invalid host clock cannot make a fresh claim look old enough to reap."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "fresh-revision"
    namespace, released, _messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )
    fresh_ts = time.time() - 1
    _replace_fresh_claim(
        tmp_path,
        owner=owner,
        claim_revision=revision,
        timestamp=datetime.datetime.fromtimestamp(fresh_ts, tz=datetime.timezone.utc).isoformat(),
    )
    report_ts = time.time() + 3600 if reported_at == "future" else float("nan")
    for host in ("chiap01", "chiap02", "chiap03"):
        (tmp_path / "live" / f"{host}.json").write_text(
            json.dumps({"host": host, "ts": report_ts, "cards": []}) + "\n",
            encoding="utf-8",
        )
    live_namespace = _load_functions("live_report_health", "live_report")
    live_namespace.update(
        {
            "LIVE": str(tmp_path / "live"),
            "LIVE_FRESH": 1800,
            "LIVE_TIMER_CYCLE": 360,
            "ROTATION_HOSTS": ("chiap01", "chiap02", "chiap03"),
            "time": time,
        }
    )
    namespace["event_rows"] = namespace["_acts_fresh_rows"]
    namespace["live_report"] = live_namespace["live_report"]

    health = namespace["live_report"]()
    assert health["oldest"] == 0.0
    assert health["running"] == set()
    assert health["reporting"] == set()
    assert {fault["reason"] for fault in health["faults"]} == {"invalid"}
    assert namespace["reap_dead_claims"]() == 0
    assert released == []


def test_conflicting_existing_outcome_id_prevents_release(tmp_path: Path) -> None:
    """An event ID collision cannot stand in for the canonical worker verdict."""
    owner = "pi-codex-chiap02-deadbeef"
    revision = "orphaned-revision"
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=revision,
        launch_revision=revision,
    )
    identity = f"deadbeef\0{owner}\0{revision}"
    verdict_id = hashlib.sha256(("fleet-reap-verdict-v1\0" + identity).encode()).hexdigest()
    event_dir = tmp_path / "card_events"
    event_dir.mkdir()
    conflicting = {
        "event_id": verdict_id,
        "card_id": "deadbeef",
        "action": "verdict",
        "verdict": "PASS",
        "writer": "fleet-liveness-reaper",
        "ts": "2026-08-31T00:00:00+00:00",
    }
    (event_dir / "fleet-liveness-reaper.jsonl").write_text(
        json.dumps(conflicting, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_OUTCOME_FAILED|") for message in messages)


@pytest.mark.parametrize(
    "existing",
    [
        "\n",
        "not-json\n",
        json.dumps({"event_id": "duplicate"})
        + "\n"
        + json.dumps({"event_id": "duplicate"})
        + "\n",
        json.dumps({"event_id": "partial"}),
    ],
    ids=("blank", "malformed", "duplicate", "partial"),
)
def test_malformed_existing_outcome_file_fails_closed(tmp_path: Path, existing: str) -> None:
    """Malformed or partial reaper evidence never authorizes a release."""
    namespace, _released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner="pi-codex-chiap02-deadbeef",
        claim_revision="revision-1",
        launch_revision="revision-1",
    )
    event_dir = tmp_path / "card_events"
    event_dir.mkdir()
    (event_dir / "fleet-liveness-reaper.jsonl").write_text(existing, encoding="utf-8")

    assert not namespace["_record_reap_outcome"](
        "deadbeef", "pi-codex-chiap02-deadbeef", "revision-1", time.time() - 900
    )
    assert any(message.startswith("REAP_OUTCOME_FAILED|") for message in messages)


def test_outcome_failure_prevents_release(tmp_path: Path) -> None:
    """The supervisor never makes a dead claim indistinguishable from no claim."""
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner="pi-codex-chiap02-deadbeef",
        claim_revision="revision-1",
        launch_revision="revision-1",
    )
    namespace["_record_reap_outcome"] = lambda *_args: False

    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert not any(message.startswith("REAPED|") for message in messages)


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
            "jarvis",
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
    namespace["_load_ineffective"] = lambda: [
        {
            "card_id": "deadbeef",
            "owner": owner,
            "claim_revision": revision,
            "failure_class": "release_command_failed",
            "runtime_version": "0.1.63",
            "timestamp": "2026-08-28T18:00:00+00:00",
        }
    ]
    assert namespace["reap_dead_claims"]() == 0
    assert released == []

    namespace["_load_ineffective"] = lambda: []
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
