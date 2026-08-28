"""Regression tests for exact fleet launch provenance in the fast reaper."""

from __future__ import annotations

import ast
import datetime
import glob
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


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
        "datetime": datetime,
        "glob": glob,
        "json": json,
        "os": os,
    }
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def _reaper_fixture(
    tmp_path: Path,
    *,
    card_id: str,
    owner: str,
    claim_revision: str,
    launch_revision: str | None,
) -> tuple[dict[str, object], list[list[str]], list[str]]:
    """Build one isolated claimed card and a fake release command."""
    cards = tmp_path / "cards"
    card = cards / card_id
    events = card / "events"
    events.mkdir(parents=True)
    (card / "core.json").write_text("{}\n", encoding="utf-8")
    claim = {
        "action": "claim",
        "claim_revision": claim_revision,
        "event_id": "claim-event",
        "owner": owner,
        "seq": 0,
        "ts": datetime.datetime.fromtimestamp(
            time.time() - 900, tz=datetime.timezone.utc
        ).isoformat(),
    }
    (events / "claim.jsonl").write_text(json.dumps(claim) + "\n", encoding="utf-8")

    live = tmp_path / "live"
    live.mkdir()
    for host in ("chiap01", "chiap02", "chiap03"):
        (live / f"{host}.json").write_text("{}\n", encoding="utf-8")

    evidence = tmp_path / "evidence"
    actions = evidence / "20260828T180000Z" / "actions.log"
    actions.parent.mkdir(parents=True)
    if launch_revision is not None:
        actions.write_text(
            f"LAUNCHED|chiap02|codex-auto-{card_id}|{card_id}|lane=codex"
            f"|model=sk-codex|owner={owner}|claim_revision={launch_revision}\n",
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


def test_successful_launch_records_exact_claim_generation() -> None:
    """Only a successful launch renders owner and revision provenance."""
    fields = _load_functions("_launch_claim_fields")["_launch_claim_fields"]
    assert fields("pi-codex-chiap02-deadbeef", "revision-1", True) == (
        "|owner=pi-codex-chiap02-deadbeef|claim_revision=revision-1"
    )
    assert fields("pi-codex-chiap02-deadbeef", "revision-1", False) == ""
    assert fields("pi-codex-chiap02-deadbeef", "", True) == ""


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
    namespace["_current_claim_identity_fresh"] = lambda _card: (
        "pi-codex-chiap03-deadbeef",
        time.time() - 900,
        "replacement-revision",
    )
    assert namespace["reap_dead_claims"]() == 0
    assert released == []
