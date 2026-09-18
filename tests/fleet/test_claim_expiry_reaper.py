"""The reaper's second release path: an absolute idle deadline.

`reap_dead_claims()` releases a claim only after every authoritative host
reports the owner's process absent. That predicate is unsatisfiable for most
owners in the store, which is why 349 claims on chi were held with an
owner-idle minimum of 30.9 hours and nothing would ever return them to the
pool. `_expire_idle_claims()` is the other half, and the tests here pin the
three properties that make it worth adding:

  1. it is off unless a human turns it on, and report mode releases nothing;
  2. it reaches owners the existing path cannot, in particular a bare
     `jarvis`, which `_parse_worker_owner()` skips before any liveness logic
     (that gate alone accounts for 146 of the 349 held claims);
  3. it never releases a generation the absence path already released, and it
     never trusts a zero exit code as proof the claim moved.

The helper is lifted out of the shipped script with `ast`, the way
`tests/test_reaper_stall.py` does, so these tests exercise the source that
runs on the fleet rather than a paraphrase of it. Importing the script itself
is not an option: it acquires the rotation lock and drives a full pass at
module scope.
"""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

from skcapstone.fleet.claim_expiry import ClaimObservation

SRC = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "skfleet-rotate.py"

HELPERS = ("_claim_ttl_release_cmd", "_claim_ttl_fresh_state", "_expire_idle_claims")

HOUR = 3600.0


def _module() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


class _Result:
    """Stand-in for a `subprocess.CompletedProcess`."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _load(tmp_path: Path, *, dry: bool = False) -> dict:
    """Exec the new helpers alone, with the script's globals stubbed."""
    tree = _module()
    wanted = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in HELPERS
    ]
    missing = set(HELPERS) - {node.name for node in wanted}
    assert not missing, f"helpers missing from the script: {sorted(missing)}"

    logged: list[str] = []
    namespace = {
        "os": os,
        "time": time,
        "Path": Path,
        "HOME": str(tmp_path),
        "HOST": "chiap01",
        "SKC": "/home/test/.skenv/bin/skcapstone",
        "DRY": dry,
        "d": str(tmp_path / "evidence"),
        "log": lambda _d, msg: logged.append(msg),
        "subprocess": None,  # every test injects a runner instead
        "_rows": {},
        "_claim_rows": {},
        "lifecycle_state": lambda cid: "claimed",
    }
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(SRC), "exec"), namespace)
    namespace["logged"] = logged
    return namespace


def _observation(owner: str = "jarvis", *, idle_hours: float = 96.0, cid: str = "aaaa0001"):
    return ClaimObservation(
        card_id=cid,
        owner=owner,
        claim_revision="rev-" + cid,
        last_owner_event_at=time.time() - idle_hours * HOUR,
    )


def _claimed_then_open():
    """A `state()` stub that answers "claimed" once per card, then "open".

    Per card rather than per call: a stub driven by a flat call sequence
    passes or fails on how many times the helper happens to read the fold,
    which is exactly the detail these tests must not pin.
    """
    seen: set[str] = set()

    def state(cid: str) -> str:
        if cid in seen:
            return "open"
        seen.add(cid)
        return "claimed"

    return state


@pytest.fixture
def helper(tmp_path):
    return _load(tmp_path)


# ---- the switch --------------------------------------------------------------


def test_expiry_path_is_off_by_default(helper, monkeypatch):
    """Nothing reclaims until a human sets the mode. This is the default."""
    monkeypatch.delenv("SKFLEET_CLAIM_TTL_MODE", raising=False)
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](observations=[_observation()], runner=calls.append)
    assert released == 0
    assert calls == []
    assert helper["logged"] == [], "off means silent, not merely harmless"


def test_off_never_reads_the_store(helper, monkeypatch):
    """Off returns before `observe()` walks every card on disk.

    The store has thousands of cards and the reaper runs every five minutes on
    three hosts, so the default must cost nothing at all.
    """
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "off")
    from skcapstone.fleet import claim_expiry

    def exploding_observe(home):
        raise AssertionError("observe() ran in off mode")

    monkeypatch.setattr(claim_expiry, "observe", exploding_observe)
    assert helper["_expire_idle_claims"](runner=lambda cmd: _Result()) == 0


def test_an_unknown_mode_is_off_not_enforce(helper, monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "yes-please")
    calls: list[list[str]] = []
    assert helper["_expire_idle_claims"](observations=[_observation()], runner=calls.append) == 0
    assert calls == []


def test_report_mode_logs_but_releases_nothing(helper, monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "report")
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](
        observations=[_observation(idle_hours=163.8)], runner=calls.append
    )
    assert released == 0
    assert calls == [], "report mode must not touch the board"
    would = [line for line in helper["logged"] if "CLAIM_TTL_WOULD_RECLAIM" in line]
    assert len(would) == 1
    assert "aaaa0001" in would[0] and "jarvis" in would[0]
    assert "163.8" in would[0], "the report has to say how idle the claim is"


def test_dry_run_never_releases_even_in_enforce(tmp_path, monkeypatch):
    """A dry run must be safe to run at any time, by anyone."""
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    helper = _load(tmp_path, dry=True)
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](observations=[_observation()], runner=calls.append)
    assert released == 0
    assert calls == []
    assert any("dry_run" in line for line in helper["logged"])


# ---- enforcement ------------------------------------------------------------


def test_enforce_mode_releases_with_cas_fence(helper, monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](
        observations=[_observation()],
        runner=lambda cmd: calls.append(cmd) or _Result(),
        state=_claimed_then_open(),
    )
    assert released == 1
    assert len(calls) == 1
    cmd = calls[0]
    assert "release-claim" in cmd
    assert "--expected-claim-revision" in cmd
    assert cmd[cmd.index("--expected-claim-revision") + 1] == "rev-aaaa0001"
    assert "--owner" in cmd
    assert cmd[cmd.index("--owner") + 1] == "jarvis"
    assert "aaaa0001" in cmd


def test_a_bare_session_owner_is_reclaimable(helper, monkeypatch):
    """Success criterion 4, and the whole reason this path exists.

    `_parse_worker_owner()` recognizes only `pi-<lane>-<host>-<cid>` shaped
    owners and skips every other owner unconditionally, before any liveness
    logic runs. `jarvis` (104 claims), `codex` (28) and `seraph` (5) are
    unreachable by the existing path for that reason alone. If this path
    routed through the same gate it would reproduce the bug it is fixing.
    """
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    owners = ["jarvis", "codex", "seraph", "codex-w72-backend-r3"]
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](
        observations=[
            _observation(owner=owner, cid="card%04d" % i) for i, owner in enumerate(owners)
        ],
        runner=lambda cmd: calls.append(cmd) or _Result(),
        state=_claimed_then_open(),
    )
    assert released == len(owners)
    assert [cmd[cmd.index("--owner") + 1] for cmd in calls] == owners


def test_a_claim_inside_the_ttl_is_left_alone(helper, monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](
        observations=[_observation(idle_hours=30.9)],
        runner=lambda cmd: calls.append(cmd) or _Result(),
    )
    assert released == 0
    assert calls == []


def test_the_ttl_is_configuration(helper, monkeypatch):
    """`SKFLEET_CLAIM_TTL_H` tightens the deadline without a code change."""
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_H", "24")
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](
        observations=[_observation(idle_hours=30.9)],
        runner=lambda cmd: calls.append(cmd) or _Result(),
        state=_claimed_then_open(),
    )
    assert released == 1


def test_a_claim_with_no_revision_is_never_released(helper, monkeypatch):
    """No fence, no release: without the CAS a racing re-claim loses its card."""
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](
        observations=[
            ClaimObservation(
                card_id="aaaa0009",
                owner="jarvis",
                claim_revision=None,
                last_owner_event_at=time.time() - 500 * HOUR,
            )
        ],
        runner=lambda cmd: calls.append(cmd) or _Result(),
    )
    assert released == 0
    assert calls == []


# ---- never both paths, never trust the exit code ----------------------------


def test_enforce_skips_a_card_the_absence_path_already_released(helper, monkeypatch):
    """The two paths must never both release the same generation.

    This path runs after `reap_dead_claims()` in the same tick, from an
    observation taken before it. Re-reading the fold immediately before the
    release is what keeps the second release from firing on a card the
    absence path already returned to the pool.
    """
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](
        observations=[_observation()],
        runner=lambda cmd: calls.append(cmd) or _Result(),
        state=lambda cid: "open",
    )
    assert released == 0
    assert calls == [], "the absence path had already released it"
    assert any("CLAIM_TTL_SKIPPED" in line for line in helper["logged"])


def test_a_zero_exit_is_not_proof_the_claim_moved(helper, monkeypatch):
    """When the two stores disagree the CLI prints success and writes nothing.

    Confirmed live on card `f17d9e32`: `release-claim` printed "Released claim
    on f17d9e32" and appended no event, because the claim was already gone.
    """
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    calls: list[list[str]] = []
    released = helper["_expire_idle_claims"](
        observations=[_observation()],
        runner=lambda cmd: calls.append(cmd) or _Result(returncode=0),
        state=lambda cid: "claimed",  # still claimed after a "successful" release
    )
    assert len(calls) == 1, "the release was attempted"
    assert released == 0, "a zero exit code counted as a reclaim"
    assert any("CLAIM_TTL_INEFFECTIVE" in line for line in helper["logged"])


def test_a_failed_release_is_logged_and_not_counted(helper, monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    released = helper["_expire_idle_claims"](
        observations=[_observation()],
        runner=lambda cmd: _Result(returncode=1, stderr="expected-claim-revision mismatch"),
        state=_claimed_then_open(),
    )
    assert released == 0
    failed = [line for line in helper["logged"] if "CLAIM_TTL_FAILED" in line]
    assert len(failed) == 1
    assert "mismatch" in failed[0]


def test_one_bad_card_does_not_stop_the_rest(helper, monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(cmd)
        return _Result(returncode=1 if "card0000" in cmd else 0)

    released = helper["_expire_idle_claims"](
        observations=[_observation(cid="card0000"), _observation(cid="card0001")],
        runner=runner,
        state=_claimed_then_open(),
    )
    assert released == 1
    assert len(calls) == 2


# ---- structural guards ------------------------------------------------------


def _helper_source() -> str:
    tree = _module()
    nodes = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in HELPERS
    ]
    # Without this the guards below pass vacuously on an empty string, which
    # is the same "tested mechanism that is reachable by nothing" failure the
    # packaging test exists to catch.
    assert {node.name for node in nodes} == set(HELPERS)
    return "\n".join(ast.unparse(node) for node in nodes)


@pytest.mark.parametrize(
    "forbidden",
    ["_parse_worker_owner", "live_report", "REAP_QUORUM", "CLAIM_GRACE"],
)
def test_the_expiry_path_depends_on_no_absence_proof(forbidden):
    """An absolute deadline needs no host to prove absence of anything.

    Routing this path through the owner-shape gate, the quorum, or the
    cross-host visibility flag would reproduce exactly the unsatisfiable
    predicate that left 349 claims held.
    """
    assert forbidden not in _helper_source()


def test_the_expiry_path_is_wired_into_the_rotation():
    """A mechanism nothing calls is the failure this project keeps repeating.

    The call sits beside `reap_dead_claims()` in the non-dry branch rather
    than inside it, so the absence path is byte-identical and the new path
    still runs when that function returns early for quorum shortage.
    """
    tree = _module()
    guard = next(
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and getattr(node.test, "id", "") == "DRY"
        and any(
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and getattr(stmt.value.func, "id", "") == "reap_dead_claims"
            for stmt in node.orelse
        )
    )
    called = {
        getattr(stmt.value.func, "id", "")
        for stmt in guard.orelse
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
    }
    assert "_expire_idle_claims" in called


def test_reap_dead_claims_is_untouched_by_this_change():
    """Success criterion 5, enforced structurally rather than by inspection.

    The absence path must release exactly what it released before. Nothing in
    this task belongs inside that function, so nothing in it names the new
    helpers.
    """
    tree = _module()
    reaper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "reap_dead_claims"
    )
    body = ast.unparse(reaper)
    for name in HELPERS:
        assert name not in body
    assert "claim_expiry" not in body


def test_off_mode_returns_without_importing_the_package(tmp_path, monkeypatch):
    """Default-off must be safe on a HALF-DEPLOYED host.

    This helper is called unguarded between reap_dead_claims() and the
    review-and-close phases. The import is lazy so module scope stays safe,
    but that alone is not enough: a host carrying the new script with an
    older package would reap, raise ImportError here, and never reach
    open_provisional_reviews or close_reviewed_parents. The same class of
    trap already bit this script once through the eager GATED_EXIT_CODE
    import, and the only protection then was deploy ordering.

    So off mode must return BEFORE the import runs. This test makes the
    import genuinely impossible and asserts off mode still returns 0.
    """
    import builtins

    loaded = _load(tmp_path)
    expire = loaded["_expire_idle_claims"]

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if "claim_expiry" in name:
            raise ImportError(f"simulated half-deployed host: no {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    monkeypatch.delenv("SKFLEET_CLAIM_TTL_MODE", raising=False)

    assert expire(env={}) == 0, "off mode must not import anything"
    assert expire(env={"SKFLEET_CLAIM_TTL_MODE": "off"}) == 0
    assert expire(env={"SKFLEET_CLAIM_TTL_MODE": "banana"}) == 0

    # And prove the stub is real: an enabled mode DOES hit the import.
    with pytest.raises(ImportError):
        expire(env={"SKFLEET_CLAIM_TTL_MODE": "report"})
