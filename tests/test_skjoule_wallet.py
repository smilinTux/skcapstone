"""Tests for JouleWallet: the Joule economy's per-agent ledger.

This is the first test coverage this money-handling code has ever had.
Every test operates against a tmp_path wallet directory only. Nothing
here may touch ~/.skcapstone/agents/ -- those directories hold real
wallets with real transaction history and are strictly read-only outside
this suite.

Sections:
    TestMint / TestSpend / TestTransfer -- characterize existing
        mint/spend/transfer semantics (values in, values out).
    TestSnapshotLedgerConsistency -- snapshot and jsonl agree after a
        sequence of operations, and balance_after matches the running
        total on every transaction.
    TestAtomicPersistence -- joules.json survives a crash mid-write and
        is never observed torn or empty.
    TestCorruptSnapshotIsLoud -- a corrupt or unreadable snapshot raises
        instead of silently resetting the balance to zero.
    TestReplayBalance -- the read-only ledger-replay primitive.
    TestAuditWallets -- the read-only cross-wallet audit primitive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.skjoule import (
    JouleWallet,
    TransactionKind,
    WalletCorruptionError,
    audit_wallets,
    replay_balance,
)


def _wallet(tmp_path: Path, agent: str = "test-agent") -> JouleWallet:
    return JouleWallet(agent, home=tmp_path)


def _state_path(tmp_path: Path, agent: str = "test-agent") -> Path:
    return tmp_path / "agents" / agent / "wallet" / "joules.json"


def _log_path(tmp_path: Path, agent: str = "test-agent") -> Path:
    return tmp_path / "agents" / agent / "wallet" / "transactions.jsonl"


def _read_log_lines(tmp_path: Path, agent: str = "test-agent") -> list[dict]:
    path = _log_path(tmp_path, agent)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestMint:
    def test_mint_increases_balance(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        wallet.mint(100, description="test work")
        assert wallet.balance == 100
        assert wallet.total_minted == 100

    def test_mint_appends_transaction(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        wallet.mint(50, description="unit of work", proof_hash="abc123")
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1
        assert lines[0]["kind"] == TransactionKind.MINT.value
        assert lines[0]["amount"] == 50
        assert lines[0]["description"] == "unit of work"
        assert lines[0]["proof_hash"] == "abc123"

    def test_mint_zero_or_negative_raises(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        with pytest.raises(ValueError):
            wallet.mint(0)
        with pytest.raises(ValueError):
            wallet.mint(-10)

    def test_mint_returns_transaction_with_balance_after(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        txn = wallet.mint(30)
        assert txn.balance_after == 30
        txn2 = wallet.mint(20)
        assert txn2.balance_after == 50


class TestSpend:
    def test_spend_decreases_balance(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        wallet.mint(100)
        wallet.spend(40, description="paid for a thing")
        assert wallet.balance == 60
        assert wallet.total_spent == 40

    def test_spend_beyond_balance_raises(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        wallet.mint(10)
        with pytest.raises(ValueError):
            wallet.spend(11)
        # Balance is unchanged after the failed spend.
        assert wallet.balance == 10

    def test_spend_zero_or_negative_raises(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        wallet.mint(10)
        with pytest.raises(ValueError):
            wallet.spend(0)
        with pytest.raises(ValueError):
            wallet.spend(-5)

    def test_spend_appends_transaction(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        wallet.mint(100)
        wallet.spend(25, description="spent it")
        lines = _read_log_lines(tmp_path)
        assert lines[-1]["kind"] == TransactionKind.SPEND.value
        assert lines[-1]["amount"] == 25
        assert lines[-1]["balance_after"] == 75


class TestTransfer:
    def test_transfer_moves_value_between_wallets(self, tmp_path: Path):
        sender = _wallet(tmp_path, "sender")
        receiver = _wallet(tmp_path, "receiver")
        sender.mint(100)

        sender.transfer(receiver, 30, description="payment")

        assert sender.balance == 70
        assert receiver.balance == 30

    def test_transfer_writes_both_sides(self, tmp_path: Path):
        sender = _wallet(tmp_path, "sender")
        receiver = _wallet(tmp_path, "receiver")
        sender.mint(100)

        send_txn, recv_txn = sender.transfer(receiver, 30, description="payment")

        assert send_txn.kind == TransactionKind.TRANSFER_OUT
        assert send_txn.amount == 30
        assert send_txn.counterparty == "receiver"
        assert recv_txn.kind == TransactionKind.TRANSFER_IN
        assert recv_txn.amount == 30
        assert recv_txn.counterparty == "sender"

        sender_lines = _read_log_lines(tmp_path, "sender")
        receiver_lines = _read_log_lines(tmp_path, "receiver")
        assert sender_lines[-1]["kind"] == TransactionKind.TRANSFER_OUT.value
        assert receiver_lines[-1]["kind"] == TransactionKind.TRANSFER_IN.value

    def test_transfer_insufficient_balance_raises(self, tmp_path: Path):
        sender = _wallet(tmp_path, "sender")
        receiver = _wallet(tmp_path, "receiver")
        sender.mint(10)
        with pytest.raises(ValueError):
            sender.transfer(receiver, 11)
        assert sender.balance == 10
        assert receiver.balance == 0

    def test_transfer_to_self_raises(self, tmp_path: Path):
        wallet = _wallet(tmp_path, "solo")
        wallet.mint(10)
        with pytest.raises(ValueError):
            wallet.transfer(wallet, 5)


class TestSnapshotLedgerConsistency:
    def test_snapshot_matches_ledger_after_sequence(self, tmp_path: Path):
        alice = _wallet(tmp_path, "alice")
        bob = _wallet(tmp_path, "bob")

        alice.mint(200)
        alice.spend(50)
        alice.transfer(bob, 30)
        bob.mint(10)
        bob.spend(5)

        alice_lines = _read_log_lines(tmp_path, "alice")
        bob_lines = _read_log_lines(tmp_path, "bob")

        alice_expected = sum(
            line["amount"] if line["kind"] in ("mint", "transfer_in") else -line["amount"]
            for line in alice_lines
        )
        bob_expected = sum(
            line["amount"] if line["kind"] in ("mint", "transfer_in") else -line["amount"]
            for line in bob_lines
        )

        assert alice.balance == alice_expected == 120
        assert bob.balance == bob_expected == 35

        alice_snapshot = json.loads(_state_path(tmp_path, "alice").read_text(encoding="utf-8"))
        bob_snapshot = json.loads(_state_path(tmp_path, "bob").read_text(encoding="utf-8"))
        assert alice_snapshot["balance"] == alice_expected
        assert bob_snapshot["balance"] == bob_expected

    def test_balance_after_matches_running_total(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        wallet.mint(100)
        wallet.spend(20)
        wallet.mint(5)
        wallet.spend(60)

        lines = _read_log_lines(tmp_path)
        running = 0
        for line in lines:
            if line["kind"] in ("mint", "transfer_in"):
                running += line["amount"]
            else:
                running -= line["amount"]
            assert line["balance_after"] == running
        assert running == 25
        assert wallet.balance == 25


class TestAtomicPersistence:
    def test_no_temp_files_left_behind(self, tmp_path: Path):
        wallet = _wallet(tmp_path)
        wallet.mint(10)
        wallet_dir = _state_path(tmp_path).parent
        names = [p.name for p in wallet_dir.iterdir()]
        assert "joules.json" in names
        assert not [n for n in names if n.endswith(".tmp") or ".tmp." in n]

    def test_reload_after_normal_write_preserves_balance(self, tmp_path: Path):
        wallet = _wallet(tmp_path, "durable")
        wallet.mint(75)
        wallet.spend(15)

        reloaded = JouleWallet("durable", home=tmp_path)
        assert reloaded.balance == 60

    def test_snapshot_write_failure_does_not_leave_a_torn_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A crash mid-write must never leave a truncated joules.json.

        This is the failure mode that made the corrupt-snapshot bug
        possible in the first place: a torn file gets read back as
        invalid JSON. The atomic writer must leave the prior good
        snapshot fully intact when the write is interrupted.
        """
        wallet = _wallet(tmp_path, "crashy")
        wallet.mint(40)
        good_snapshot = _state_path(tmp_path, "crashy").read_text(encoding="utf-8")

        import os as os_module

        def boom(*_args, **_kwargs):
            raise OSError("simulated disk failure mid-write")

        monkeypatch.setattr(os_module, "fsync", boom)

        # Journal-before-state (2026-08-16): the fsync that fails first is now
        # the JOURNAL's, so the transaction raises instead of being swallowed.
        # That is the point of the change, and the snapshot must be left
        # completely untouched rather than merely untorn: a balance must never
        # advance past a transaction that could not be written down.
        with pytest.raises(OSError):
            wallet.mint(10)

        assert _state_path(tmp_path, "crashy").read_text(encoding="utf-8") == good_snapshot
        wallet_dir = _state_path(tmp_path, "crashy").parent
        assert not [p for p in wallet_dir.iterdir() if ".tmp" in p.name]


class TestCorruptSnapshotIsLoud:
    def test_corrupt_json_raises_instead_of_zeroing(self, tmp_path: Path):
        agent = "corrupt-agent"
        wallet_dir = tmp_path / "agents" / agent / "wallet"
        wallet_dir.mkdir(parents=True)
        (wallet_dir / "joules.json").write_text("{not valid json!!", encoding="utf-8")
        (wallet_dir / "transactions.jsonl").write_text(
            json.dumps(
                {
                    "kind": "mint",
                    "amount": 500,
                    "counterparty": "economy",
                    "description": "real money, not gone",
                    "proof_hash": "",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "balance_after": 500,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with pytest.raises(WalletCorruptionError) as exc_info:
            JouleWallet(agent, home=tmp_path)

        assert "transactions.jsonl" in str(exc_info.value)
        # The corrupt file itself must not have been overwritten with a
        # fresh zero-balance snapshot as a side effect of construction.
        assert (wallet_dir / "joules.json").read_text(encoding="utf-8") == "{not valid json!!"

    def test_empty_snapshot_file_raises(self, tmp_path: Path):
        agent = "empty-agent"
        wallet_dir = tmp_path / "agents" / agent / "wallet"
        wallet_dir.mkdir(parents=True)
        (wallet_dir / "joules.json").write_text("", encoding="utf-8")

        with pytest.raises(WalletCorruptionError):
            JouleWallet(agent, home=tmp_path)

    def test_missing_snapshot_creates_fresh_zero_wallet(self, tmp_path: Path):
        """A wallet that has never existed is not "corrupt": zero is correct."""
        wallet = JouleWallet("brand-new-agent", home=tmp_path)
        assert wallet.balance == 0


class TestReplayBalance:
    def test_replay_matches_snapshot(self, tmp_path: Path):
        wallet = _wallet(tmp_path, "replay-agent")
        wallet.mint(100)
        wallet.spend(30)
        wallet.mint(10)

        assert replay_balance("replay-agent", home=tmp_path) == wallet.balance == 80

    def test_replay_across_transfer(self, tmp_path: Path):
        sender = _wallet(tmp_path, "replay-sender")
        receiver = _wallet(tmp_path, "replay-receiver")
        sender.mint(100)
        sender.transfer(receiver, 40)

        assert replay_balance("replay-sender", home=tmp_path) == sender.balance == 60
        assert replay_balance("replay-receiver", home=tmp_path) == receiver.balance == 40

    def test_replay_never_writes_anything(self, tmp_path: Path):
        wallet = _wallet(tmp_path, "readonly-agent")
        wallet.mint(50)
        state_path = _state_path(tmp_path, "readonly-agent")
        before = state_path.stat().st_mtime_ns

        replay_balance("readonly-agent", home=tmp_path)

        assert state_path.stat().st_mtime_ns == before

    def test_replay_missing_agent_is_zero(self, tmp_path: Path):
        assert replay_balance("never-existed", home=tmp_path) == 0

    def test_replay_does_not_require_a_readable_snapshot(self, tmp_path: Path):
        """Replay must work even when JouleWallet() itself would raise."""
        agent = "corrupt-but-replayable"
        wallet_dir = tmp_path / "agents" / agent / "wallet"
        wallet_dir.mkdir(parents=True)
        (wallet_dir / "joules.json").write_text("garbage", encoding="utf-8")
        (wallet_dir / "transactions.jsonl").write_text(
            json.dumps(
                {
                    "kind": "mint",
                    "amount": 250,
                    "counterparty": "economy",
                    "description": "",
                    "proof_hash": "",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "balance_after": 250,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert replay_balance(agent, home=tmp_path) == 250


class TestAuditWallets:
    def test_audit_reports_agreement(self, tmp_path: Path):
        wallet = _wallet(tmp_path, "healthy-agent")
        wallet.mint(100)
        wallet.spend(20)

        results = audit_wallets(home=tmp_path)
        matches = [r for r in results if r.agent == "healthy-agent"]
        assert len(matches) == 1
        result = matches[0]
        assert result.snapshot_balance == 80
        assert result.replayed_balance == 80
        assert result.agrees is True
        assert result.error == ""

    def test_audit_reports_disagreement_without_repairing(self, tmp_path: Path):
        wallet = _wallet(tmp_path, "tampered-agent")
        wallet.mint(100)

        # Simulate a corrupted-but-parseable snapshot: valid JSON, wrong
        # number. The audit must report it, not fix it.
        state_path = _state_path(tmp_path, "tampered-agent")
        data = json.loads(state_path.read_text(encoding="utf-8"))
        data["balance"] = 999999
        state_path.write_text(json.dumps(data), encoding="utf-8")

        results = audit_wallets(home=tmp_path)
        result = next(r for r in results if r.agent == "tampered-agent")
        assert result.snapshot_balance == 999999
        assert result.replayed_balance == 100
        assert result.agrees is False

        # Audit is read-only: the tampered value must still be on disk.
        after = json.loads(state_path.read_text(encoding="utf-8"))
        assert after["balance"] == 999999

    def test_audit_reports_unreadable_snapshot_without_raising(self, tmp_path: Path):
        agent = "broken-agent"
        wallet_dir = tmp_path / "agents" / agent / "wallet"
        wallet_dir.mkdir(parents=True)
        (wallet_dir / "joules.json").write_text("{bad", encoding="utf-8")
        (wallet_dir / "transactions.jsonl").write_text(
            json.dumps(
                {
                    "kind": "mint",
                    "amount": 42,
                    "counterparty": "economy",
                    "description": "",
                    "proof_hash": "",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "balance_after": 42,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        results = audit_wallets(home=tmp_path)
        result = next(r for r in results if r.agent == agent)
        assert result.snapshot_balance is None
        assert result.replayed_balance == 42
        assert result.agrees is False
        assert result.error != ""

    def test_audit_no_agents_dir_returns_empty(self, tmp_path: Path):
        assert audit_wallets(home=tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# Journal-before-state ordering (2026-08-16)
# ---------------------------------------------------------------------------


def test_journal_is_written_before_the_snapshot(tmp_path, monkeypatch):
    """The transaction must be durable before the balance is published.

    Order is the entire guarantee here: the two writes cannot be atomic with
    each other, so the survivor on failure has to be the one a replay can
    repair from. A snapshot written first leaves an advanced balance with no
    transaction explaining it, which nothing can reconstruct.
    """
    from skcapstone import skjoule

    w = JouleWallet("order-probe", home=tmp_path)
    w.mint(100, description="seed")

    order: list[str] = []
    real_atomic = skjoule.atomic_write_text

    def spy_atomic(path, text):
        order.append("state")
        return real_atomic(path, text)

    monkeypatch.setattr(skjoule, "atomic_write_text", spy_atomic)

    log_path = tmp_path / "agents" / "order-probe" / "wallet" / "transactions.jsonl"
    real_open = type(log_path).open

    def spy_open(self, *a, **kw):
        if self == log_path and "a" in str(a[0] if a else kw.get("mode", "")):
            order.append("journal")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(type(log_path), "open", spy_open)
    w.spend(10, description="probe")

    assert order[:2] == ["journal", "state"], f"wrong order: {order}"


def test_failed_journal_does_not_advance_the_balance(tmp_path, monkeypatch):
    """A transaction that cannot be journaled must not move the balance.

    This is the live failure that produced 3 drifted wallets: the append was
    best-effort and swallowed, so a lost write left the snapshot ahead of the
    log forever. It must raise, and the balance must be unchanged on disk AND
    in memory.
    """
    w = JouleWallet("journal-fail", home=tmp_path)
    w.mint(500, description="seed")
    before = w.balance

    log_path = tmp_path / "agents" / "journal-fail" / "wallet" / "transactions.jsonl"
    real_open = type(log_path).open

    def boom(self, *a, **kw):
        if self == log_path and "a" in str(a[0] if a else kw.get("mode", "")):
            raise OSError("disk full")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(type(log_path), "open", boom)

    with pytest.raises(OSError):
        w.spend(100, description="should not land")

    # in-memory rolled back to the last consistent state
    assert w.balance == before
    # and a freshly opened wallet agrees
    assert JouleWallet("journal-fail", home=tmp_path).balance == before
    # and the replay agrees with both, which is the invariant that broke
    assert replay_balance("journal-fail", home=tmp_path) == before


def test_reconcile_makes_replay_agree_without_moving_the_balance(tmp_path):
    """Reconciliation writes the correction down; it does not adjust silently.

    And it must NOT move the balance: using mint()/spend() would advance the
    snapshot too and re-open exactly the gap it is closing. Chef's ruling is
    that the snapshot is authoritative, so the journal is what gets corrected.
    """
    from skcapstone.skjoule import reconcile_wallet

    w = JouleWallet("drifty", home=tmp_path)
    w.mint(1000, description="seed")

    # forge the live defect: snapshot ahead of the journal by 500
    log = tmp_path / "agents" / "drifty" / "wallet" / "transactions.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    rows[-1]["amount"] = 500
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    assert replay_balance("drifty", home=tmp_path) == 500
    before = JouleWallet("drifty", home=tmp_path).balance
    assert before == 1000

    dry = reconcile_wallet("drifty", home=tmp_path, dry_run=True)
    assert dry["delta"] == 500 and dry["written"] is False
    assert replay_balance("drifty", home=tmp_path) == 500, "dry run must write nothing"

    done = reconcile_wallet("drifty", home=tmp_path, dry_run=False)
    assert done["written"] is True
    assert replay_balance("drifty", home=tmp_path) == before
    assert JouleWallet("drifty", home=tmp_path).balance == before, "balance must not move"

    # the correction is legible in the journal, not silent
    last = json.loads(log.read_text().splitlines()[-1])
    assert "RECONCILIATION" in last["description"]
    assert last["counterparty"] == "reconciliation"
