"""A provisional PASS must be recorded as hash-bound native candidate evidence.

MEASURED ON THE LIVE chi BOARD, 2026-09-18. ``OPENED_REVIEW`` was 0 across 14
days and 1,660 rotations, while 214 cards reported
``OPEN_REVIEW_EVIDENCE_BLOCKED`` every single cycle. Of 354 cards that had ever
been reported blocked that way, 305 carried their ``PASS_FOR_REVIEW`` ONLY as a
kanban overlay row of the exact shape

    {"action": "link", "link_key": "verdict", "link_value": "PASS_FOR_REVIEW",
     "event_id": null, "seq": 0, "column": null, ...}

which is byte-for-byte what ``skcapstone coord link <card> verdict
PASS_FOR_REVIEW`` writes, and 346 of the 354 carried no hash-bound candidate
evidence anywhere on the card, in either store.

That is not worker sloppiness. ``coord link`` builds a ``CardEvent``, a pydantic
model with a fixed field set that has no slot for ``candidate_path`` or
``candidate_sha256``, so a verdict written through it STRUCTURALLY cannot carry
the binding a governed review requires. And ``coord link`` was the only verdict
command the fleet had: the worker brief, ``AGENTS.md``, ``coord briefing`` and
``coord --help`` all named it and nothing else.

So the producer gets the command it was missing, and the command that cannot
carry the binding refuses the verdict class that needs one.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from skcapstone.card import CardEvent, CardEventLog
from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

COMMIT = "a" * 40
TREE = "b" * 40
REF = "refs/heads/fix/candidate"


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(home: Path, card_id: str) -> None:
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id=card_id, title="Card", description="body"))
    CardStore(home).create(CardCore(id=card_id, title="Card", description="body"))


def _candidate(home: Path, card_id: str) -> Path:
    path = home / "evidence" / "work" / card_id / "candidate.patch"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("diff --git a/x b/x\n", encoding="utf-8")
    return path


def _run(home: Path, *args: str):
    return CliRunner().invoke(_main(), ["coord", *args, "--home", str(home)])


def _native_events(home: Path, card_id: str) -> list[dict]:
    directory = home / "cards" / card_id / "events"
    rows: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else ():
        rows += [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
    return rows


def _overlay_events(home: Path, card_id: str) -> list[dict]:
    directory = home / "coordination" / "card_events"
    rows: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else ():
        rows += [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
    return [row for row in rows if row.get("card_id") == card_id]


# ── the review opener, read out of the deployed launcher ──────────────────────

_FUNCTIONS = {
    "_fold_key",
    "_blocked_reason",
    "_native_outcome_value",
    "_outcome_event_value",
    "_event_sort_key",
    "_event_identity",
    "_matching_outcome_events",
    "_generation_invalidated",
    "_provisional_candidate",
    "_parent_review_generation",
    "_outcome_scan_rows",
}
_CONSTANTS = {
    "_OUTCOME_KEYS",
    "_OUTCOME_VALUE_RE",
    "_PIPE_OUTCOME_RE",
    "_INVALID_NATIVE_OUTCOME",
    "_PROVISIONAL_PASS_RE",
}


def _opener(home: Path) -> dict[str, object]:
    """Bind the real opener seam to the real bytes the CLI just wrote."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & _CONSTANTS:
                nodes.append(node)
    namespace: dict[str, object] = {
        "hashlib": hashlib,
        "json": json,
        "os": __import__("os"),
        "re": re,
        "event_rows": lambda cid: _native_events(home, cid),
        "_load_evidence_events": lambda: {
            cid: _overlay_events(home, cid)
            for cid in {row["card_id"] for row in _overlay_events_all(home)}
        },
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


def _overlay_events_all(home: Path) -> list[dict]:
    directory = home / "coordination" / "card_events"
    rows: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else ():
        rows += [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
    return [row for row in rows if row.get("card_id")]


# ── the defect, and the fix ───────────────────────────────────────────────────


def test_the_overlay_row_the_214_carry_can_never_open_a_review(tmp_path: Path):
    """The 214, reproduced from the event ``coord link`` used to write.

    Written through ``CardEventLog`` directly because ``coord link`` now refuses
    this verdict class. These are the live bytes: every optional field null,
    ``event_id`` null, ``seq`` 0, and nowhere to put a candidate.
    """
    _seed(tmp_path, "aaaa1111")
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id="aaaa1111",
            action="link",
            link_key="verdict",
            link_value="PASS_FOR_REVIEW",
            writer="pi-codex-test",
        )
    )
    row = next(r for r in _overlay_events(tmp_path, "aaaa1111") if r.get("link_key"))
    assert set(row) == {
        "card_id",
        "action",
        "event_id",
        "writer",
        "ts",
        "seq",
        "column",
        "order",
        "priority",
        "swimlane",
        "label",
        "link_key",
        "link_value",
        "owner",
        "title",
        "description",
    }
    assert "candidate_sha256" not in row and "candidate_path" not in row
    opener = _opener(tmp_path)
    assert opener["_parent_review_generation"]("aaaa1111", row["ts"], "PASS_FOR_REVIEW") is None


def test_coord_verdict_writes_native_hash_bound_candidate_evidence(tmp_path: Path):
    _seed(tmp_path, "bbbb2222")
    candidate = _candidate(tmp_path, "bbbb2222")
    result = _run(
        tmp_path,
        "verdict",
        "bbbb2222",
        "PASS_FOR_REVIEW",
        "--candidate",
        str(candidate),
        "--commit",
        COMMIT,
        "--tree",
        TREE,
        "--ref",
        REF,
        "--agent",
        "pi-codex-test",
    )
    assert result.exit_code == 0, result.output
    event = next(e for e in _native_events(tmp_path, "bbbb2222") if e.get("action") == "verdict")
    assert event["verdict"] == "PASS_FOR_REVIEW"
    assert event["writer"] == "pi-codex-test"
    assert event["candidate_path"] == str(candidate.resolve())
    assert event["candidate_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert (event["candidate_commit"], event["candidate_tree"], event["candidate_ref"]) == (
        COMMIT,
        TREE,
        REF,
    )
    # The fold is the only authority on card state.
    assert CardStore(tmp_path).fold("bbbb2222") is not None


def test_the_opener_admits_a_verdict_it_previously_refused(tmp_path: Path):
    """End to end: the same card, recorded the new way, yields a generation."""
    _seed(tmp_path, "cccc3333")
    candidate = _candidate(tmp_path, "cccc3333")
    result = _run(
        tmp_path,
        "verdict",
        "cccc3333",
        "PASS_FOR_REVIEW",
        "--candidate",
        str(candidate),
        "--commit",
        COMMIT,
        "--tree",
        TREE,
        "--ref",
        REF,
        "--agent",
        "pi-codex-test",
    )
    assert result.exit_code == 0, result.output
    event = next(e for e in _native_events(tmp_path, "cccc3333") if e.get("action") == "verdict")
    opener = _opener(tmp_path)
    generation = opener["_parent_review_generation"]("cccc3333", event["ts"], "PASS_FOR_REVIEW")
    assert generation is not None
    _digest, producer, path, digest, commit, tree, ref = (generation[0],) + generation[1:]
    assert producer == "pi-codex-test"
    assert path == str(candidate.resolve())
    assert digest == hashlib.sha256(candidate.read_bytes()).hexdigest()
    # generation[4] is the typed candidate_commit the opener needs to bind a
    # governed review card to a real source revision. Without it the opener
    # logs OPEN_REVIEW_SOURCE_UNBOUND and skips.
    assert (commit, tree, ref) == (COMMIT, TREE, REF)


def test_coord_link_refuses_a_provisional_pass_and_names_the_verb(tmp_path: Path):
    _seed(tmp_path, "dddd4444")
    result = _run(
        tmp_path, "link", "dddd4444", "verdict", "PASS_FOR_REVIEW", "--agent", "pi-codex-test"
    )
    assert result.exit_code != 0
    assert "coord verdict" in result.output
    assert not [r for r in _overlay_events(tmp_path, "dddd4444") if r.get("link_key") == "verdict"]


def test_coord_link_still_accepts_a_plain_pass(tmp_path: Path):
    """The refusal is scoped to the verdict class that needs a binding."""
    _seed(tmp_path, "eeee5555")
    result = _run(tmp_path, "link", "eeee5555", "verdict", "PASS", "--agent", "pi-codex-test")
    assert result.exit_code == 0, result.output
    assert [r for r in _overlay_events(tmp_path, "eeee5555") if r.get("link_key") == "verdict"]


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"commit": "nope"}, "40-character"),
        ({"tree": "nope"}, "40-character"),
        ({"ref": "my-branch"}, "refs/heads/"),
    ],
)
def test_coord_verdict_refuses_an_unusable_source_binding(tmp_path: Path, override, expected):
    _seed(tmp_path, "ffff6666")
    candidate = _candidate(tmp_path, "ffff6666")
    args = {"commit": COMMIT, "tree": TREE, "ref": REF, **override}
    result = _run(
        tmp_path,
        "verdict",
        "ffff6666",
        "PASS_FOR_REVIEW",
        "--candidate",
        str(candidate),
        "--commit",
        args["commit"],
        "--tree",
        args["tree"],
        "--ref",
        args["ref"],
        "--agent",
        "pi-codex-test",
    )
    assert result.exit_code != 0
    assert expected in result.output
    assert not [e for e in _native_events(tmp_path, "ffff6666") if e.get("action") == "verdict"]


def test_coord_verdict_refuses_a_candidate_that_is_not_there(tmp_path: Path):
    """A SHA with no reachable bytes is a promise that expired, not evidence."""
    _seed(tmp_path, "9999aaaa")
    result = _run(
        tmp_path,
        "verdict",
        "9999aaaa",
        "PASS_FOR_REVIEW",
        "--candidate",
        str(tmp_path / "gone.patch"),
        "--commit",
        COMMIT,
        "--tree",
        TREE,
        "--ref",
        REF,
        "--agent",
        "pi-codex-test",
    )
    assert result.exit_code != 0
    assert "candidate" in result.output.lower()


def test_coord_verdict_requires_a_named_producer(tmp_path: Path):
    """``_provisional_candidate`` fails closed on an empty writer, so refuse early."""
    _seed(tmp_path, "8888bbbb")
    candidate = _candidate(tmp_path, "8888bbbb")
    result = _run(
        tmp_path,
        "verdict",
        "8888bbbb",
        "PASS_FOR_REVIEW",
        "--candidate",
        str(candidate),
        "--commit",
        COMMIT,
        "--tree",
        TREE,
        "--ref",
        REF,
    )
    assert result.exit_code != 0
    assert "--agent" in result.output


def test_later_work_invalidates_the_generation_so_the_verdict_goes_last(tmp_path: Path):
    """The ordering trap, made explicit.

    ``_generation_invalidated`` treats any later event on the card as work that
    supersedes the outcome, so a generation is only current while nothing
    follows it. That makes the verdict the LAST write on the card, after the
    branch and commit_sha links DEFINITION OF DONE asks for. Not hypothetical:
    measured on chiap01, 262 of 355 cards blocked at the review gate already
    carry post-verdict events of exactly this kind, which is why re-verdicting
    them natively is cleaner than retro-attaching evidence to a stale
    generation. The brief, the verb help and the refusal message all say so.

    Asserted with a later NATIVE event, because whether a later OVERLAY event
    invalidates depends on how wide the staleness scan is, and that is being
    changed separately.
    """
    _seed(tmp_path, "7777cccc")
    candidate = _candidate(tmp_path, "7777cccc")
    assert (
        _run(
            tmp_path,
            "verdict",
            "7777cccc",
            "PASS_FOR_REVIEW",
            "--candidate",
            str(candidate),
            "--commit",
            COMMIT,
            "--tree",
            TREE,
            "--ref",
            REF,
            "--agent",
            "pi-codex-test",
        ).exit_code
        == 0
    )
    event = next(e for e in _native_events(tmp_path, "7777cccc") if e.get("action") == "verdict")
    assert _opener(tmp_path)["_parent_review_generation"](
        "7777cccc", event["ts"], "PASS_FOR_REVIEW"
    )

    later = _candidate(tmp_path, "7777cccc").with_name("second.patch")
    later.write_text("later work\n", encoding="utf-8")
    assert (
        _run(
            tmp_path,
            "verdict",
            "7777cccc",
            "PASS_FOR_REVIEW",
            "--candidate",
            str(later),
            "--commit",
            COMMIT,
            "--tree",
            TREE,
            "--ref",
            REF,
            "--agent",
            "pi-codex-test",
        ).exit_code
        == 0
    )
    assert (
        _opener(tmp_path)["_parent_review_generation"]("7777cccc", event["ts"], "PASS_FOR_REVIEW")
        is None
    )
