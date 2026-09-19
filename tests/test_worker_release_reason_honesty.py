"""``not-abandoned`` must name a finish that actually happened.

MEASURED ON CHI, 2026-09-19. Eight cards were frozen at the claim ceiling on
chiap01. Between them their ledgers carried well over a hundred
``release_claim`` events and every one was labelled ``not-abandoned``, which
``coord release-claim --help`` and ``skcoord/abandon_reason.py`` both define as
"the release follows a durable finish, not a stoppage". Reading the same cards'
events found no verdict, no PASS, no candidate commit and no evidence of any
kind. Card 63d0474d, for one, holds 14 claims, 14 releases all marked
``not-abandoned``, and zero outcome events.

The releases were written by ``finalize_worker_exit`` on the way out of every
worker, with the reason hardcoded. So the field asserted success unconditionally
and the collapse ``abandon_reason.py`` exists to prevent -- a success release
becoming indistinguishable from a release nobody can explain -- happened anyway,
in the opposite direction: every stoppage became indistinguishable from a
success. An operator reading the ledger is told these workers finished.

The fix does not invent an outcome. It claims ``not-abandoned`` only when this
claim generation actually recorded a terminal verdict in one of the two stores a
verdict can land in, and otherwise writes ``unspecified``, whose whole purpose is
to say the cause is not known.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"

FUNCTIONS = {
    "durable_verdict_recorded",
    "_fold_link_key",
    "_claim_opened_at",
    "_overlay_rows",
}
CONSTANTS = {"_OUTCOME_LINK_KEYS", "_OUTCOME_VALUE_RE"}

CARD = "63d0474d"
SEAT = "pi-glm-chiap02-63d0474d"
REV = "9d409d48b38448cd9cbbbeb71a36dbdf"


def _ns() -> dict:
    """Extract the pure verdict-detection seam without running the wrapper."""
    tree = ast.parse(WRAPPER.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & CONSTANTS:
                nodes.append(node)
    namespace = {"re": re, "json": json, "Path": Path}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(WRAPPER), "exec"), namespace)
    missing = (FUNCTIONS | CONSTANTS) - namespace.keys()
    assert not missing, f"release-reason seam missing from wrapper: {sorted(missing)}"
    return namespace


def _claim(ts: str, revision: str = REV) -> dict:
    return {
        "action": "claim",
        "writer": SEAT,
        "owner": SEAT,
        "claim_revision": revision,
        "ts": ts,
    }


def _overlay(tmp_path: Path, rows: list[dict]) -> Path:
    home = tmp_path / ".skcapstone"
    events = home / "coordination" / "card_events"
    events.mkdir(parents=True)
    (events / "chiap02.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return home


# ----------------------------------------------------------------------
# The measured defect.
# ----------------------------------------------------------------------


def test_the_measured_63d0474d_shape_is_not_a_durable_finish(tmp_path):
    """14 claim/release pairs, zero outcome events. This is not success."""
    events = [_claim("2026-09-19T13:30:00+00:00")]
    home = _overlay(
        tmp_path,
        [
            {
                "card_id": CARD,
                "action": "link",
                "writer": "niobe",
                "link_key": "worker_liveness",
                "link_value": f"{SEAT}|{REV}|active",
                "ts": "2026-09-19T13:35:00+00:00",
            }
        ],
    )
    ns = _ns()
    assert ns["durable_verdict_recorded"](home, CARD, events, REV) is False


def test_a_native_verdict_event_in_the_shard_is_a_durable_finish(tmp_path):
    events = [
        _claim("2026-09-19T13:30:00+00:00"),
        {
            "action": "verdict",
            "writer": SEAT,
            "verdict": "PASS_FOR_REVIEW",
            "ts": "2026-09-19T13:44:00+00:00",
        },
    ]
    ns = _ns()
    assert ns["durable_verdict_recorded"](_overlay(tmp_path, []), CARD, events, REV)


def test_an_outcome_link_in_the_overlay_is_a_durable_finish(tmp_path):
    events = [_claim("2026-09-19T13:30:00+00:00")]
    home = _overlay(
        tmp_path,
        [
            {
                "card_id": CARD,
                "action": "link",
                "writer": SEAT,
                "link_key": "result",
                "link_value": "PASS 15/15 classified: A=0 B=7 C=8 D=0",
                "ts": "2026-09-19T13:44:00+00:00",
            }
        ],
    )
    ns = _ns()
    assert ns["durable_verdict_recorded"](home, CARD, events, REV) is True


# ----------------------------------------------------------------------
# The fences.
# ----------------------------------------------------------------------


def test_an_older_generations_verdict_does_not_credit_this_one(tmp_path):
    """A card that passed yesterday did not pass under this claim."""
    events = [
        {
            "action": "verdict",
            "writer": SEAT,
            "verdict": "PASS",
            "ts": "2026-09-18T09:00:00+00:00",
        },
        _claim("2026-09-19T13:30:00+00:00"),
    ]
    ns = _ns()
    assert ns["durable_verdict_recorded"](_overlay(tmp_path, []), CARD, events, REV) is False


def test_an_unfindable_claim_generation_is_not_credited(tmp_path):
    """No fence means no claim of success, not a free pass."""
    events = [
        _claim("2026-09-19T13:30:00+00:00", revision="some-other-revision"),
        {
            "action": "verdict",
            "writer": SEAT,
            "verdict": "PASS",
            "ts": "2026-09-19T13:44:00+00:00",
        },
    ]
    ns = _ns()
    assert ns["durable_verdict_recorded"](_overlay(tmp_path, []), CARD, events, REV) is False


def test_a_non_outcome_link_is_not_a_verdict(tmp_path):
    """verdict_artifact is a path, not an outcome. Neither is a classify note."""
    events = [_claim("2026-09-19T13:30:00+00:00")]
    home = _overlay(
        tmp_path,
        [
            {
                "card_id": CARD,
                "action": "link",
                "writer": SEAT,
                "link_key": "verdict_artifact",
                "link_value": "evidence/work/63d0474d/report.md",
                "ts": "2026-09-19T13:40:00+00:00",
            },
            {
                "card_id": CARD,
                "action": "link",
                "writer": SEAT,
                "link_key": "classify:0dd8dde0",
                "link_value": "A: the BLOCKED verdict is stale",
                "ts": "2026-09-19T13:41:00+00:00",
            },
        ],
    )
    ns = _ns()
    assert ns["durable_verdict_recorded"](home, CARD, events, REV) is False


def test_another_cards_verdict_does_not_leak_across(tmp_path):
    events = [_claim("2026-09-19T13:30:00+00:00")]
    home = _overlay(
        tmp_path,
        [
            {
                "card_id": "724c2e52",
                "action": "link",
                "writer": SEAT,
                "link_key": "verdict",
                "link_value": "PASS_FOR_REVIEW",
                "ts": "2026-09-19T13:44:00+00:00",
            }
        ],
    )
    ns = _ns()
    assert ns["durable_verdict_recorded"](home, CARD, events, REV) is False


def test_an_unreadable_overlay_reports_unknown_not_success(tmp_path):
    """A store that will not read is not evidence that the work finished."""
    home = tmp_path / ".skcapstone"
    (home / "coordination").mkdir(parents=True)
    ns = _ns()
    assert ns["durable_verdict_recorded"](
        home, CARD, [_claim("2026-09-19T13:30:00+00:00")], REV
    ) is False


def test_the_release_call_no_longer_hardcodes_not_abandoned():
    """The literal must not reappear at the release site on a later edit."""
    source = WRAPPER.read_text(encoding="utf-8")
    assert 'abandon_reason="not-abandoned"' not in source
    assert "abandon_reason=reason," in source
    assert 'else "unspecified"' in source
