"""Task 1 of the seat-depinning plan: does the claim fence exclude a second host?

This module adds no production code. It characterises existing behaviour so the
rest of the plan (which removes the static ``active_host`` election and lets any
host in an estate run any seat) can decide whether that is safe.

The plan's premise is that "the CardStore claim fence, which is exact-revision
fenced, already prevents two hosts from doing the same work twice"
(see ``lifecycle_seats.load_seat_control_plane``). The two tests below exercise
the actual production claim primitive, ``skcoord.coordination.Board.claim_task``
(the function ``skcapstone coord claim`` calls, which is what
``scripts/fleet/skfleet-rotate.py``'s ``ONLY_SEAT`` dispatch path shells out to
before it admits a worker for a card):

- Against ONE physical CardStore directory, two processes racing to claim the
  same card correctly exclude one another: exactly one wins. The mechanism is
  ``skcoord.card_store.card_mutation_lock``, an ``fcntl.flock`` held on the
  card's own directory for the whole fold-check-append sequence inside
  ``Board.claim_task`` / ``Board._claim_task``.

- Against TWO independently seeded CardStore directories holding byte-identical
  pre-race state (the shape of two hosts' ``~/.skcapstone`` immediately after a
  Syncthing round trip, before either has claimed anything), the same claim
  race against the same card ID succeeds on BOTH sides. ``fcntl.flock`` is
  kernel-local: it has no reach across two independent directory trees, and
  nothing in the claim path checks a second host's store before admitting.
  This is the actual estate topology
  (``~/.skcapstone`` is one Syncthing folder per host, synced asynchronously,
  not a shared live filesystem), so this is the scenario the plan needs to hold
  and it does not.

A third, lighter test pins down that ``scripts/fleet/skfleet-rotate.py``'s own
last-line-of-defense admission gate, ``acquire_card_admission``, documents the
same host-local scope in its own docstring, independent of the claim path.

Conclusion: the fence does NOT hold across two independently synced hosts. It
only holds when both racers share one literal CardStore filesystem, which is
not how an estate's hosts actually relate to each other. See
``task-1-report.md`` for the full writeup.
"""

from __future__ import annotations

import json
import multiprocessing
import shutil
from pathlib import Path

from skcapstone.card_store import CardStore
from skcapstone.coordination import Board, Task

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _race_claim(home: str, agent: str, task_id: str, barrier, out_path: str) -> None:
    """Attempt one claim after synchronizing with the other racer.

    Runs in its own process (a stand-in for a second host) so the race is a
    real concurrent filesystem race, not two calls serialized by the GIL.
    """
    barrier.wait()
    try:
        Board(Path(home)).claim_task(agent, task_id)
        outcome = {"agent": agent, "won": True, "error": None}
    except ValueError as exc:
        outcome = {"agent": agent, "won": False, "error": str(exc)}
    Path(out_path).write_text(json.dumps(outcome), encoding="utf-8")


def _run_race(home_a: Path, home_b: Path, task_id: str, tmp_path: Path) -> tuple[dict, dict]:
    barrier = multiprocessing.Barrier(2)
    out_a = tmp_path / "host_a_result.json"
    out_b = tmp_path / "host_b_result.json"
    proc_a = multiprocessing.Process(
        target=_race_claim,
        args=(str(home_a), "pi-tank-chiap01-" + task_id, task_id, barrier, str(out_a)),
    )
    proc_b = multiprocessing.Process(
        target=_race_claim,
        args=(str(home_b), "pi-tank-chiap02-" + task_id, task_id, barrier, str(out_b)),
    )
    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=30)
    proc_b.join(timeout=30)
    assert proc_a.exitcode == 0, "host_a racer crashed"
    assert proc_b.exitcode == 0, "host_b racer crashed"
    return (
        json.loads(out_a.read_text(encoding="utf-8")),
        json.loads(out_b.read_text(encoding="utf-8")),
    )


def test_claim_fence_excludes_second_claimant_sharing_one_cardstore(tmp_path: Path) -> None:
    """Two hosts racing for the same card against ONE CardStore: one wins."""
    home = tmp_path / "estate"
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id="c1", title="dispatch target"))

    result_a, result_b = _run_race(home, home, "c1", tmp_path)

    winners = [result for result in (result_a, result_b) if result["won"]]
    losers = [result for result in (result_a, result_b) if not result["won"]]
    assert len(winners) == 1, (result_a, result_b)
    assert len(losers) == 1, (result_a, result_b)
    assert "already" in losers[0]["error"], losers[0]

    card = CardStore(home).fold("c1")
    assert card is not None
    assert card.owner == winners[0]["agent"]


def test_claim_fence_does_not_span_independently_synced_per_host_stores(
    tmp_path: Path,
) -> None:
    """Two hosts, each with their OWN synced CardStore copy: both win locally.

    This is the real estate topology: ``~/.skcapstone`` is a per-host Syncthing
    folder, not a shared live filesystem. Seed two homes with byte-identical
    state (one fully settled sync round trip, before either host claims
    anything), then race the same claim against each independently. Nothing
    in ``Board.claim_task`` looks at the other host's store, so both succeed.
    """
    home_a = tmp_path / "host_a" / "estate"
    board = Board(home_a)
    board.ensure_dirs()
    board.create_task(Task(id="c1", title="dispatch target"))

    home_b = tmp_path / "host_b" / "estate"
    shutil.copytree(home_a, home_b)

    result_a, result_b = _run_race(home_a, home_b, "c1", tmp_path)

    # Both racers believe they alone hold the claim: the fence that holds
    # against one shared store (previous test) has no effect once the store
    # itself is no longer shared.
    assert result_a["won"] is True, result_a
    assert result_b["won"] is True, result_b

    card_a = CardStore(home_a).fold("c1")
    card_b = CardStore(home_b).fold("c1")
    assert card_a is not None and card_b is not None
    assert card_a.owner == "pi-tank-chiap01-c1"
    assert card_b.owner == "pi-tank-chiap02-c1"
    assert card_a.owner != card_b.owner


def test_dispatch_admission_gate_documents_itself_as_host_local() -> None:
    """skfleet-rotate.py's own admission primitive scopes itself to one host.

    ``acquire_card_admission`` is the last check before a worker process is
    spawned for a claimed card (ONLY_SEAT dispatch path). Its own docstring
    already says what the two tests above prove operationally: this gate is
    per-host, not per-estate.
    """
    source = ROTATE.read_text(encoding="utf-8")
    assert "Atomically admit one worker for an exact card on this host." in source
    assert "os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)" in source
