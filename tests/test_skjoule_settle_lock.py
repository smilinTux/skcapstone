"""The settlement lock: concurrent wallet writers must not lose updates.

WHAT THIS FILE IS EVIDENCE FOR.

``JouleEngine.record_work`` reads the balance (``JouleWallet.__init__`` loading
the snapshot), adds to it, and writes it back. Two writers used to run that
sequence unsynchronised, so both captured the same balance and the second write
erased the first. The live ``lumina`` wallet lost a 25 J credit and a 50 J
credit exactly this way, both of them ``auto_tokenize_task`` entries.

The two race tests below are deterministic, not opportunistic. Each writer is
held at a rendezvous placed immediately after it has READ the balance and before
it writes, so neither can write until both have read. Against the unfixed code
that ordering is guaranteed to lose an update, so the tests are red every run,
not "usually". Against the fixed code the rendezvous cannot be satisfied (the
second writer is still queued for the lock and has not read anything yet), it
times out, and the writers serialise. The assertion is on the resulting balance
and journal either way.

``TestPositiveControl`` is the other half of the argument: a lock that made
every settlement after the first a silent no-op would pass the race tests too.

``TestCrossRepoLockPath`` is the reason this lock works at all. skharness
settles against the SAME wallet files from its own process, and two processes
holding two DIFFERENT locks over one wallet protect nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import skcapstone.skjoule as sj
from skcapstone.skjoule import (
    SETTLE_LOCK_NAME,
    JouleEngine,
    JouleWallet,
    ProductionWalletInTestError,
    _settle_lock_path,
    settle_lock,
)

AGENT = "race-agent"
SEED = 100
STEP = 25

#: How long a writer waits at the rendezvous for its peer. Only ever reached on
#: the FIXED code, where the peer is queued for the lock and cannot arrive, so
#: this is the price of the green path and nothing else.
RENDEZVOUS_TIMEOUT = 2.0


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _seed(home: Path) -> None:
    """Give the wallet a starting balance, before any gate is installed."""
    JouleWallet(AGENT, home=home).mint(SEED, description="seed")


def _state(home: Path) -> dict:
    path = home / "agents" / AGENT / "wallet" / "joules.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _journal(home: Path) -> list[dict]:
    path = home / "agents" / AGENT / "wallet" / "transactions.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# --------------------------------------------------------------------------- #
# The lock path is a cross-repo contract                                       #
# --------------------------------------------------------------------------- #


def _load_skharness_joules():
    """Import skharness's joule module, or None when it cannot check the contract.

    skharness is deliberately NOT a dependency of skcapstone (the arrow runs the
    other way), so this is a soft import. A skharness that predates the settle
    lock (it landed in skharness 0.3.15) cannot verify the cross-repo half of
    the contract either, so it counts as unavailable here.
    """
    try:
        import skharness.autocode.joules as hj
    except ImportError:
        return None
    required = ("SETTLE_LOCK_NAME", "SETTLE_LOCK_TIMEOUT", "_settle_lock_path", "settle_lock")
    if not all(hasattr(hj, name) for name in required):
        return None
    return hj


SKHARNESS_JOULES = _load_skharness_joules()

#: Why the cross-repo tests skip when skharness cannot verify the contract.
SKHARNESS_UNAVAILABLE = (
    "skharness with the settle-lock contract (>= 0.3.15) is not installed here, "
    "so the cross-repo half of the lock contract is unverified in this environment"
)


class TestCrossRepoLockPath:
    """skharness and skcapstone must contend for the same file, byte for byte."""

    def test_lock_sits_beside_the_wallet_it_guards(self, tmp_path: Path):
        """A lock elsewhere could end up guarding a different wallet."""
        wallet_dir = tmp_path / "agents" / AGENT / "wallet"
        assert _settle_lock_path(AGENT, tmp_path) == wallet_dir / SETTLE_LOCK_NAME
        assert SETTLE_LOCK_NAME == ".settle.lock"

    @pytest.mark.skipif(SKHARNESS_JOULES is None, reason=SKHARNESS_UNAVAILABLE)
    def test_resolution_matches_skharness_byte_for_byte(self, tmp_path: Path):
        """The literal check above pins skcapstone; this pins the pair.

        skharness is deliberately NOT a dependency of skcapstone (the arrow runs
        the other way), so this cannot be a hard import. It runs wherever both
        repos are installed, which is every dev box on this fleet, and it is the
        only thing that turns a silent drift in either repo into a red test
        rather than an unprotected wallet.
        """
        hj = SKHARNESS_JOULES
        assert hj is not None  # guaranteed by the skipif above

        assert sj.SETTLE_LOCK_NAME == hj.SETTLE_LOCK_NAME
        assert sj.SETTLE_LOCK_TIMEOUT == hj.SETTLE_LOCK_TIMEOUT

        for agent in (AGENT, "lumina", "opus"):
            for home in (tmp_path, tmp_path / "nested", None):
                mine = _settle_lock_path(agent, home)
                theirs = hj._settle_lock_path(agent, home)
                assert str(mine) == str(theirs), (
                    f"lock path drift for agent={agent!r} home={home!r}: "
                    f"skcapstone {mine} vs skharness {theirs}. Two locks over one "
                    f"wallet protect nothing."
                )

    @pytest.mark.skipif(SKHARNESS_JOULES is None, reason=SKHARNESS_UNAVAILABLE)
    def test_skharness_and_skcapstone_actually_exclude_each_other(self, tmp_path: Path):
        """Equal paths are the mechanism; mutual exclusion is the property.

        Two locks that merely resolve to the same string but were opened with
        different flags, or on different inodes, would still let both writers
        through. This holds skharness's lock and checks that skcapstone's is
        refused, then checks the reverse.
        """
        hj = SKHARNESS_JOULES
        assert hj is not None  # guaranteed by the skipif above

        with hj.settle_lock(AGENT, home=tmp_path) as theirs:
            assert theirs is True
            with settle_lock(AGENT, home=tmp_path, timeout=0.3) as mine:
                assert mine is False, "skcapstone took a lock skharness was holding"

        with settle_lock(AGENT, home=tmp_path) as mine:
            assert mine is True
            with hj.settle_lock(AGENT, home=tmp_path, timeout=0.3) as theirs:
                assert theirs is False, "skharness took a lock skcapstone was holding"


# --------------------------------------------------------------------------- #
# The race, in threads                                                         #
# --------------------------------------------------------------------------- #


class TestConcurrentRecordWork:
    def test_two_threads_do_not_lose_an_update(self, tmp_path: Path, monkeypatch):
        """Both writers read the same balance before either writes.

        The barrier sits inside the snapshot load, so on the unfixed code both
        writers are guaranteed to hold the stale balance when they mint and the
        second write is guaranteed to erase the first: 100 + 25 + 25 lands as
        125. On the fixed code the second writer is still queued for the flock
        and never reaches the barrier, it breaks on timeout, and the writers
        serialise to 150.

        Two engines, not one, because the lock has to work across separate open
        file descriptions. flock is keyed on the description rather than the
        process, so two threads that each open the file contend exactly as two
        processes do; a lock that only worked within one engine would be a
        mutex wearing a lock file's name.
        """
        _seed(tmp_path)

        gate = threading.Barrier(2)
        original_load = JouleWallet._load_or_create_snapshot

        def gated_load(self):
            snapshot = original_load(self)
            try:
                gate.wait(timeout=RENDEZVOUS_TIMEOUT)
            except threading.BrokenBarrierError:
                pass  # the fixed path: the peer is queued for the lock
            return snapshot

        monkeypatch.setattr(JouleWallet, "_load_or_create_snapshot", gated_load)

        errors: list[BaseException] = []

        def writer(tag: str):
            try:
                JouleEngine(home=tmp_path).record_work(
                    worker=AGENT,
                    category="operations",
                    description=f"concurrent {tag}",
                    joules=STEP,
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
            assert not t.is_alive(), "a writer never finished; the lock is wedged"

        assert not errors, f"writer raised: {errors}"

        state = _state(tmp_path)
        assert state["balance"] == SEED + 2 * STEP, (
            f"lost update: balance is {state['balance']}, expected {SEED + 2 * STEP}. "
            f"Two writers captured the same balance and the second write erased "
            f"the first."
        )
        assert state["total_minted"] == SEED + 2 * STEP

        journal = _journal(tmp_path)
        descriptions = [t["description"] for t in journal]
        assert "concurrent a" in descriptions and "concurrent b" in descriptions
        assert sorted(t["balance_after"] for t in journal) == [SEED, SEED + STEP, SEED + 2 * STEP]

    def test_two_processes_do_not_lose_an_update(self, tmp_path: Path):
        """The same race with real interpreters, which is how it actually happens.

        Several sessions run on one box, each in its own process, so the
        contending writers are frequently not threads at all. The children are
        started and held at a parent-controlled gate BEFORE they touch the
        wallet, so interpreter startup is paid outside the rendezvous and the
        rendezvous window can stay short.
        """
        _seed(tmp_path)

        child = tmp_path / "race_child.py"
        child.write_text(
            textwrap.dedent(f"""
                import sys, time
                from pathlib import Path

                home = Path(sys.argv[1])
                me, peer = sys.argv[2], sys.argv[3]
                started = home / "started"
                ready = home / "ready"
                started.mkdir(exist_ok=True)
                ready.mkdir(exist_ok=True)

                import skcapstone.skjoule as sj

                # Signal that the import is done, then wait for the parent's go,
                # so both children enter the wallet at effectively the same time.
                (started / me).touch()
                deadline = time.monotonic() + 60
                while not (home / "GO").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)

                original_load = sj.JouleWallet._load_or_create_snapshot
                fired = []

                def gated_load(self):
                    snapshot = original_load(self)
                    if not fired:
                        fired.append(1)
                        (ready / me).touch()
                        stop = time.monotonic() + {RENDEZVOUS_TIMEOUT}
                        while not (ready / peer).exists() and time.monotonic() < stop:
                            time.sleep(0.005)
                    return snapshot

                sj.JouleWallet._load_or_create_snapshot = gated_load

                sj.JouleEngine(home=home).record_work(
                    worker={AGENT!r},
                    category="operations",
                    description="process " + me,
                    joules={STEP},
                )
                """),
            encoding="utf-8",
        )

        # Pin the child to THIS checkout. A worktree test that lets the child
        # import whatever is installed would grade the live tree, not the branch.
        src = str(Path(sj.__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONPATH=src + os.pathsep + os.environ.get("PYTHONPATH", ""))

        procs = [
            subprocess.Popen(
                [sys.executable, str(child), str(tmp_path), tag, peer],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for tag, peer in (("a", "b"), ("b", "a"))
        ]

        started = tmp_path / "started"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if started.exists() and {p.name for p in started.iterdir()} >= {"a", "b"}:
                break
            time.sleep(0.02)
        else:  # pragma: no cover - only on a wedged box
            for p in procs:
                p.kill()
            pytest.fail("children never finished importing skcapstone")
        (tmp_path / "GO").touch()

        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, f"child failed:\nstdout={out}\nstderr={err}"

        state = _state(tmp_path)
        assert state["balance"] == SEED + 2 * STEP, (
            f"lost update across processes: balance is {state['balance']}, expected "
            f"{SEED + 2 * STEP}"
        )
        assert state["total_minted"] == SEED + 2 * STEP
        descriptions = [t["description"] for t in _journal(tmp_path)]
        assert "process a" in descriptions and "process b" in descriptions


# --------------------------------------------------------------------------- #
# Positive control: the lock must not serialise everything into a no-op        #
# --------------------------------------------------------------------------- #


class TestPositiveControl:
    def test_sequential_settlements_both_land(self, tmp_path: Path):
        """A lock that dropped every write after the first would pass the race
        tests above by accident. Two sequential settlements from two separate
        engines must both reach the balance AND the journal."""
        _seed(tmp_path)

        JouleEngine(home=tmp_path).record_work(
            worker=AGENT, category="operations", description="first", joules=25
        )
        JouleEngine(home=tmp_path).record_work(
            worker=AGENT, category="operations", description="second", joules=50
        )

        state = _state(tmp_path)
        assert state["balance"] == SEED + 75
        assert state["total_minted"] == SEED + 75

        descriptions = [t["description"] for t in _journal(tmp_path)]
        assert descriptions == ["seed", "first", "second"]

    def test_repeated_settlements_on_one_engine_all_land(self, tmp_path: Path):
        """The same engine reuses its cached wallet, and reload() must not cost
        it any of its own writes."""
        engine = JouleEngine(home=tmp_path)
        for i in range(5):
            engine.record_work(worker=AGENT, category="operations", description=f"n{i}", joules=10)
        assert _state(tmp_path)["balance"] == 50
        assert len(_journal(tmp_path)) == 5

    def test_a_writer_outside_this_process_is_picked_up(self, tmp_path: Path):
        """The cached snapshot is why the balance was stale in the first place.

        An engine that settled once, then had its wallet written by somebody
        else, must not add to the balance it remembers.
        """
        engine = JouleEngine(home=tmp_path)
        engine.record_work(worker=AGENT, category="operations", description="mine", joules=10)

        # Somebody else (skharness, another session) credits the same wallet.
        JouleWallet(AGENT, home=tmp_path).mint(90, description="theirs")

        engine.record_work(worker=AGENT, category="operations", description="mine2", joules=10)
        assert _state(tmp_path)["balance"] == 110


# --------------------------------------------------------------------------- #
# Lock mechanics                                                               #
# --------------------------------------------------------------------------- #


class TestSettleLockMechanics:
    def test_lock_is_acquired_and_released(self, tmp_path: Path):
        with settle_lock(AGENT, home=tmp_path) as held:
            assert held is True
        # Released: a fresh acquisition succeeds immediately rather than
        # burning the timeout and proceeding degraded.
        start = time.monotonic()
        with settle_lock(AGENT, home=tmp_path, timeout=1.0) as held:
            assert held is True
        assert time.monotonic() - start < 1.0

    def test_nested_acquisition_on_one_thread_does_not_self_deadlock(self, tmp_path: Path):
        """flock is keyed on the open file description, so re-opening the same
        path inside a stack that already holds it would block against itself."""
        start = time.monotonic()
        with settle_lock(AGENT, home=tmp_path, timeout=1.0) as outer:
            with settle_lock(AGENT, home=tmp_path, timeout=1.0) as inner:
                assert outer is True and inner is True
        assert time.monotonic() - start < 1.0

    def test_a_held_lock_blocks_another_thread_until_release(self, tmp_path: Path):
        """The negative control for the mechanics: without this, every 'held'
        assertion above would also pass on a lock that never excluded anything."""
        entered = threading.Event()
        second_held: list[bool] = []

        def contender():
            with settle_lock(AGENT, home=tmp_path, timeout=0.3) as held:
                second_held.append(held)

        with settle_lock(AGENT, home=tmp_path) as held:
            assert held is True
            entered.set()
            t = threading.Thread(target=contender)
            t.start()
            t.join(timeout=30)

        assert second_held == [
            False
        ], "a second acquisition succeeded while the lock was held; the lock excludes nothing"
        assert entered.is_set()


# --------------------------------------------------------------------------- #
# The production-wallet guard                                                  #
# --------------------------------------------------------------------------- #


class TestProductionWalletGuard:
    def test_conftest_redirects_the_default_wallet_root(self, tmp_path: Path):
        """The autouse fixture must actually move the default off production."""
        assert Path(sj.SHARED_ROOT).expanduser().resolve() not in sj._PRODUCTION_WALLET_ROOTS

    def test_opening_a_production_wallet_from_a_test_raises(self):
        """Isolation that is merely assumed is what let 102,450 fabricated
        joules into a live wallet next door. This is the assertion."""
        production = next(iter(sj._PRODUCTION_WALLET_ROOTS))
        with pytest.raises(ProductionWalletInTestError):
            JouleWallet(AGENT, home=production)

    def test_the_guard_is_a_no_op_on_a_throwaway_root(self, tmp_path: Path):
        """Negative control: production must never be refused a settlement, and
        a guard that fired on everything would be indistinguishable from one
        that fired on the right thing."""
        wallet = JouleWallet(AGENT, home=tmp_path)
        assert wallet.balance == 0
