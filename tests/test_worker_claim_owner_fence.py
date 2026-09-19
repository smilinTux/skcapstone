"""Cross-host exclusion: a worker that does not own the folded claim must not run.

Live shape this guards (measured on chi, 2026-09-18): two hosts pass every
local pre-launch recheck because Syncthing has not yet delivered the other
host's claim, and both launch workers for the same card. Once both claims
sync, the CardStore fold names exactly one owner. The wrapper re-reads that
fold at startup and the loser exits without touching the card.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
from pathlib import Path

from skcapstone.card_store import CardCore

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"

LEGIT_OWNER = "pi-qwen-chiap01-d7a38a00"
INTERLOPER = "pi-codex-chiap04-d7a38a00"
REVISION = "ad1ace24e0e44d7ea46897c3aec9f5bb"


def _wrapper():
    spec = importlib.util.spec_from_file_location("claim_owner_fence_wrapper", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _claimed_card(module, home: Path, card: str, owner: str, revision: str) -> None:
    store = module.CardStore(home)
    store.create(CardCore(id=card, title="cross-host exclusion synthetic"))
    store.append_event(card, "claim", owner, owner=owner, claim_revision=revision)


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _args(tmp_path: Path, owner: str, command: list[str]) -> argparse.Namespace:
    return argparse.Namespace(
        owner=owner,
        card="d7a38a00",
        session="",
        claim_revision=REVISION,
        host="chiap04",
        lane="codex",
        model="fake",
        stdout=tmp_path / "worker-stdout.log",
        live_snapshot=None,
        worker_executable=sys.executable,
        startup_timeout=5.0,
        command=command,
        evidence_dir=tmp_path / "evidence" / "worker-exits",
    )


def test_interloper_aborts_without_touching_the_card(tmp_path, monkeypatch, capsys):
    """A worker whose identity is not the folded claim owner must refuse to run.

    This is the exact live pair: the card's folded claim belongs to
    pi-qwen-chiap01-d7a38a00 while pi-codex-chiap04-d7a38a00 was launched for
    it anyway. The loser must exit before any work, and must not append any
    event to the card: it does not own the claim, so it has no business
    releasing, voiding, or otherwise writing to it.
    """
    module = _wrapper()
    home = tmp_path / ".skcapstone"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "emit_work_mail", lambda *args: None)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    monkeypatch.setattr(module, "preflight_mailbox", lambda args: True)
    _claimed_card(module, home, "d7a38a00", LEGIT_OWNER, REVISION)
    marker = tmp_path / "child-ran"
    args = _args(
        tmp_path,
        INTERLOPER,
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
    )
    monkeypatch.setattr(module, "parse_args", lambda: args)
    before = _tree_digest(home)
    assert module.main() == 2
    assert _tree_digest(home) == before, "abort must not write any card event"
    assert not marker.exists(), "child command must never start"
    assert not args.stdout.exists()
    line = next(
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("ABORTED_NOT_CLAIM_OWNER|")
    )
    assert line == (
        f"ABORTED_NOT_CLAIM_OWNER|card=d7a38a00|worker={INTERLOPER}"
        f"|observed_owner={LEGIT_OWNER}"
    )
    (report,) = (tmp_path / "evidence" / "worker-startup").glob("*.json")
    assert '"state": "startup-aborted-not-claim-owner"' in report.read_text()


def test_owner_match_runs_the_child(tmp_path, monkeypatch):
    """The folded owner itself keeps running: the fence only stops interlopers."""
    module = _wrapper()
    home = tmp_path / ".skcapstone"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "emit_work_mail", lambda *args: None)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    monkeypatch.setattr(module, "preflight_mailbox", lambda args: True)
    _claimed_card(module, home, "d7a38a00", LEGIT_OWNER, REVISION)
    args = _args(tmp_path, LEGIT_OWNER, [sys.executable, "-c", "print('working')"])
    monkeypatch.setattr(module, "parse_args", lambda: args)
    assert module.main() == 0
    assert b"working" in args.stdout.read_bytes()


def test_fold_that_cannot_be_read_never_authorizes_an_abort(tmp_path, monkeypatch):
    """Observation failure proves nothing: the fence aborts only on real proof."""
    module = _wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    class BrokenStore:
        def __init__(self, home):
            del home

        def fold(self, card):
            raise OSError("store unavailable")

    monkeypatch.setattr(module, "CardStore", BrokenStore)
    args = argparse.Namespace(owner=INTERLOPER, card="d7a38a00")
    assert module.foreign_claim_owner(args) is None


def test_missing_card_never_authorizes_an_abort(tmp_path, monkeypatch):
    module = _wrapper()
    (tmp_path / ".skcapstone").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    args = argparse.Namespace(owner=INTERLOPER, card="d7a38a00")
    assert module.foreign_claim_owner(args) is None


def test_unclaimed_fold_reads_as_not_ours(tmp_path, monkeypatch):
    """A fold with no owner means this worker's claim is gone: do not run."""
    module = _wrapper()
    home = tmp_path / ".skcapstone"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    module.CardStore(home).create(CardCore(id="d7a38a00", title="unclaimed synthetic"))
    args = argparse.Namespace(owner=INTERLOPER, card="d7a38a00")
    assert module.foreign_claim_owner(args) == "unclaimed"
