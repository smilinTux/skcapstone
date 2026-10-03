"""
SKJoule -- Energy-based economic engine for sovereign agents.

Every computation carries real consequences. Joules are the unit of
useful work in the SKWorld economy. They are earned through verified
contributions and tracked with cryptographic proof.

Architecture:
    WorkCategory  -- Classification of productive work
    WorkRecord    -- A single unit of verified work
    JouleWallet   -- Per-agent Joule balance and transaction history
    XPBridge      -- Converts GTD XP into Joules via multipliers
    JouleEngine   -- Minting, spending, and P&L tracking

The economic loop:
    usage.py tracks costs  -->  coordination.py tracks tasks
           |                            |
           v                            v
    JouleEngine computes P&L    XPBridge converts completions to Joules
           |                            |
           +----> JouleWallet <---------+
                  (mint / spend / transfer)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import AGENT_HOME, SHARED_ROOT
from .atomic_io import atomic_write_text

try:  # POSIX only; the fallback in settle_lock() covers everything else.
    import fcntl
except ImportError:  # pragma: no cover - not reachable on this fleet
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("skcapstone.skjoule")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WorkCategory(str, Enum):
    """Categories of productive work in the SKWorld economy."""

    DEVELOPMENT = "development"
    BUSINESS = "business"
    COMMUNITY = "community"
    OPERATIONS = "operations"
    PHYSICAL = "physical"


class TransactionKind(str, Enum):
    """Type of Joule transaction."""

    MINT = "mint"
    SPEND = "spend"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class WorkRecord(BaseModel):
    """A single unit of verified work in the economy.

    Every minting event is backed by a WorkRecord that describes
    what was done, who did it, and the cryptographic proof hash
    tying it to an artifact (commit SHA, task ID, invoice, etc.).
    """

    worker: str = Field(description="Agent or human name that performed the work")
    category: WorkCategory = Field(description="Classification of the work")
    description: str = Field(description="Human-readable summary of what was done")
    joules: int = Field(ge=0, description="Joules earned for this work")
    proof_hash: str = Field(
        default="", description="SHA-256 hash of proof artifact (commit, task file, etc.)"
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 timestamp of when the work was recorded",
    )
    verified: bool = Field(
        default=False,
        description="Whether the proof has been independently verified",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context (task_id, commit_sha, etc.)",
    )


class Transaction(BaseModel):
    """A single ledger entry in a JouleWallet."""

    kind: TransactionKind
    amount: int = Field(ge=0, description="Joules involved in this transaction")
    counterparty: str = Field(
        default="", description="Other party (for transfers) or source (for mints)"
    )
    description: str = Field(default="", description="Human-readable note")
    proof_hash: str = Field(default="", description="Proof artifact hash")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    balance_after: int = Field(default=0, description="Wallet balance after this transaction")


class WalletSnapshot(BaseModel):
    """Serializable wallet state for persistence."""

    agent: str
    balance: int = 0
    total_minted: int = 0
    total_spent: int = 0
    total_transferred_in: int = 0
    total_transferred_out: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class PLStatement(BaseModel):
    """Profit-and-loss statement for an agent."""

    agent: str
    period: str = Field(description="Human-readable period label")
    joules_earned: int = 0
    joules_spent: int = 0
    joules_transferred_in: int = 0
    joules_transferred_out: int = 0
    net_joules: int = Field(default=0, description="Earned - Spent + TransIn - TransOut")
    llm_cost_usd: float = Field(default=0.0, description="LLM API costs from usage.py")
    current_balance: int = 0
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class NetworkStats(BaseModel):
    """Aggregate stats across all agents in the economy."""

    total_minted: int = 0
    total_spent: int = 0
    total_transfers: int = 0
    active_agents: int = 0
    agent_balances: dict[str, int] = Field(default_factory=dict)
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Wallet corruption
# ---------------------------------------------------------------------------


class WalletCorruptionError(RuntimeError):
    """Raised when a wallet snapshot exists but cannot be trusted.

    ``joules.json`` is a cache of the last known balance, not the ledger.
    ``transactions.jsonl`` is the append-only, replayable source of truth.
    A snapshot that fails to parse must never be treated as an empty
    wallet, since that would silently zero out a real balance while the
    caller believes the wallet loaded successfully. Recover with
    ``replay_balance(agent)`` against the transaction log, then repair or
    replace the snapshot file by hand.
    """

    def __init__(self, agent: str, state_path: Path, log_path: Path, detail: str) -> None:
        self.agent = agent
        self.state_path = state_path
        self.log_path = log_path
        message = (
            f"Wallet snapshot for '{agent}' at {state_path} is corrupt or "
            f"unreadable ({detail}). Refusing to silently reset the balance to "
            f"zero. {log_path} is the replayable source of truth: use "
            f"replay_balance('{agent}') to recover the true balance, then "
            f"repair or replace the snapshot file by hand."
        )
        super().__init__(message)


class ProductionWalletInTestError(RuntimeError):
    """A test run resolved a joule wallet to a production skcapstone root.

    Deliberately loud. The failure this exists to prevent is silent by nature:
    a suite that mints well formed joules into the operator's live ledger looks
    exactly like a suite that passed, and a balance that is partly pytest output
    looks exactly like a balance that is real. The sibling harness measured
    1,366 fixture mints totalling 102,450 joules reaching a live wallet before
    anyone noticed, which is the whole argument for asserting rather than
    trusting each test file to isolate itself.
    """


def _freeze_production_wallet_roots() -> frozenset[Path]:
    """Snapshot the roots holding real, operator-owned economic state."""
    roots: set[Path] = set()
    for cand in (SHARED_ROOT, AGENT_HOME, Path.home() / ".skcapstone"):
        if not cand:
            continue
        try:
            roots.add(Path(cand).expanduser().resolve())
        except OSError:
            continue
    return frozenset(roots)


#: Evaluated at import ON PURPOSE, so a test fixture that redirects the module's
#: ``SHARED_ROOT`` cannot also redirect what counts as production. A guard whose
#: definition of production moves with the thing it is guarding is not a guard.
_PRODUCTION_WALLET_ROOTS: frozenset[Path] = _freeze_production_wallet_roots()


def assert_not_production_wallet_in_test(wallet_root: Path) -> None:
    """Fail loudly if a test run is about to open a production wallet.

    A no-op outside a test run: production must never be refused a settlement.

    Args:
        wallet_root: The skcapstone root a wallet is being opened against.

    Raises:
        ProductionWalletInTestError: pytest is driving and *wallet_root* is a
            root holding real economic state.
    """
    if not (os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules):
        return
    try:
        resolved = Path(wallet_root).expanduser().resolve()
    except OSError:
        return
    if resolved in _PRODUCTION_WALLET_ROOTS:
        raise ProductionWalletInTestError(
            f"refusing to open a PRODUCTION joule wallet from a test run: {resolved}. "
            f"mint/spend are writes to real economic state, so a suite reaching this "
            f"path writes fabricated history into the operator's ledger. Pass "
            f"home=tmp_path (tests/conftest.py redirects the default for every test)."
        )


# ---------------------------------------------------------------------------
# The settlement lock
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS AND WHY IT IS NOT A MUTEX.
#
# JouleWallet already carries a ``threading.Lock``, but it is an INSTANCE
# attribute and the balance it guards is read once, in ``__init__``, into
# ``self._snapshot``. Every writer builds its own wallet, so two writers hold
# two different mutexes that never contend, each captures the same stale
# balance, and the second ``_persist()`` overwrites the first. That is a plain
# read-modify-write lost update, and it is not hypothetical: the live ``lumina``
# wallet lost a 25 J credit and a 50 J credit this way, both of them entries
# written by ``JouleEngine.auto_tokenize_task`` (description
# ``[<card_id>] Task completed: <title>``).
#
# An in-process mutex would close only half of it anyway. Several sessions run
# on one box, each in its own interpreter, so the contending writers are
# frequently different PROCESSES. ``flock`` is the cheapest primitive that
# covers both, because Linux associates the lock with the open file description
# rather than the process, so two threads that each ``open()`` the file contend
# exactly as two processes do.
#
# THE NAME AND PATH ARE A CROSS-REPO CONTRACT. skharness settles against the
# SAME wallet files from its own process (``skharness.autocode.joules.settle``),
# and two processes holding two DIFFERENT locks over one wallet protect
# nothing: both would proceed. The constants and the resolution below therefore
# mirror ``skharness.autocode.joules`` exactly, and
# ``tests/test_skjoule_settle_lock.py`` asserts the two resolved paths are
# byte-identical so a drift in either repo fails a test rather than silently
# un-protecting the wallet.

#: Lock file serialising settlements against one agent's wallet. It lives in the
#: wallet directory, beside the state and journal it protects, so a lock can
#: never end up guarding a different wallet than the one being written.
#: MUST equal ``skharness.autocode.joules.SETTLE_LOCK_NAME``.
SETTLE_LOCK_NAME = ".settle.lock"

#: How long to queue for the lock before giving up and writing anyway. The
#: critical section is a handful of small file writes, so this is orders of
#: magnitude beyond a healthy wait; reaching it means something is wedged.
#: MUST equal ``skharness.autocode.joules.SETTLE_LOCK_TIMEOUT``.
SETTLE_LOCK_TIMEOUT = 30.0

#: Fallback when fcntl is unavailable. Serialises threads within one process
#: only, which is strictly worse than flock and is why it is a fallback.
_fallback_locks: dict[str, threading.Lock] = {}
_fallback_locks_guard = threading.Lock()

#: Lock paths this THREAD already holds, so a nested acquisition is a pass
#: through instead of a self-deadlock. flock is keyed on the open file
#: description, not the process, so re-opening the same path in a call stack
#: that already holds it would block against itself until the timeout and then
#: proceed unlocked. Thread-local because the whole point of the lock is that
#: two threads must NOT share the entry.
_reentrancy = threading.local()


def _settle_lock_path(agent: str, home: Optional[Path] = None) -> Path:
    """Path of the lock file guarding *agent*'s wallet.

    Resolved through exactly the inputs :class:`JouleWallet` resolves its own
    directory through, so the lock follows the wallet wherever ``home`` sends
    it. A lock pinned to a fixed path while the wallet moved would be the
    fleet's oldest failure shape: a guard watching a different file than the
    writer touches.

    ``home is not None`` rather than a truthiness test, and the extra
    ``expanduser()``, are deliberate: they match
    ``skharness.autocode.joules._settle_lock_path`` character for character, and
    a lock that agrees with skharness on every input except the odd ones is a
    lock that protects the wallet except on the odd ones.
    """
    root = Path(home) if home is not None else Path(SHARED_ROOT).expanduser()
    return Path(root).expanduser() / "agents" / agent / "wallet" / SETTLE_LOCK_NAME


@contextlib.contextmanager
def settle_lock(agent: str, home: Optional[Path] = None, timeout: float = SETTLE_LOCK_TIMEOUT):
    """Hold exclusive write access to *agent*'s wallet, across processes.

    Yields True when the lock is held and False when it could not be taken. The
    caller proceeds either way; see the note on the timeout below.

    WHY A TIMEOUT THAT PROCEEDS RATHER THAN RAISES. Minting is credit for work
    that has already been verified, so refusing to mint DISCARDS the credit this
    lock exists to protect. Since the journal-before-state fix the journal is
    written first, so even an unlocked write leaves a durable transaction that
    ``replay_balance()`` can reconstruct from; only the cached balance is at
    risk. Waiting forever, by contrast, would hang a caller behind a stale lock
    file. So a timeout logs loudly and continues degraded.

    Args:
        agent: Wallet owner.
        home: Root skcapstone directory, or None for the SHARED_ROOT default.
        timeout: Seconds to queue before proceeding unlocked.
    """
    path = _settle_lock_path(agent, home)
    key = str(path)

    held = getattr(_reentrancy, "held", None)
    if held is None:
        held = _reentrancy.held = set()
    if key in held:
        # Already inside the critical section on this thread.
        yield True
        return

    if fcntl is None:  # pragma: no cover - POSIX everywhere on this fleet
        with _fallback_locks_guard:
            lock = _fallback_locks.setdefault(key, threading.Lock())
        acquired = lock.acquire(timeout=timeout)
        held.add(key)
        try:
            if not acquired:
                logger.warning("Settle lock (in-process fallback) timed out for %s", agent)
            yield acquired
        finally:
            held.discard(key)
            if acquired:
                lock.release()
        return

    handle = None
    acquired = False
    held.add(key)
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
        except OSError as exc:
            # An unopenable lock file must not cost a mint. Same reasoning as
            # the timeout: the journal is the durable side.
            logger.warning("Could not open settle lock %s (%s); writing unlocked", path, exc)
            yield False
            return

        deadline = time.monotonic() + max(timeout, 0.0)
        delay = 0.002
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Settle lock for %s still held after %.1fs; writing unlocked, "
                        "the journal remains authoritative",
                        agent,
                        timeout,
                    )
                    break
                time.sleep(delay)
                delay = min(delay * 2, 0.05)
        yield acquired
    finally:
        held.discard(key)
        if handle is not None:
            if acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                handle.close()


# ---------------------------------------------------------------------------
# JouleWallet
# ---------------------------------------------------------------------------


class JouleWallet:
    """Per-agent Joule balance and transaction history.

    Persists wallet state to ``~/.skcapstone/agents/{name}/wallet/joules.json``
    and an append-only transaction log at
    ``~/.skcapstone/agents/{name}/wallet/transactions.jsonl``.

    Thread-safe: all mutations are guarded by a lock.

    Args:
        agent_name: The agent this wallet belongs to.
        home: Root skcapstone directory (default from AGENT_HOME).
    """

    def __init__(self, agent_name: str, home: Optional[Path] = None) -> None:
        self._agent = agent_name
        root = Path(home) if home else Path(SHARED_ROOT).expanduser()
        assert_not_production_wallet_in_test(root)
        self._wallet_dir = root / "agents" / agent_name / "wallet"
        self._state_path = self._wallet_dir / "joules.json"
        self._log_path = self._wallet_dir / "transactions.jsonl"
        self._lock = threading.Lock()
        self._snapshot = self._load_or_create_snapshot()

    # -- Public properties ---------------------------------------------------

    @property
    def agent(self) -> str:
        """Agent name owning this wallet."""
        return self._agent

    @property
    def balance(self) -> int:
        """Current Joule balance."""
        with self._lock:
            return self._snapshot.balance

    @property
    def total_minted(self) -> int:
        """Lifetime Joules minted into this wallet."""
        with self._lock:
            return self._snapshot.total_minted

    @property
    def total_spent(self) -> int:
        """Lifetime Joules spent from this wallet."""
        with self._lock:
            return self._snapshot.total_spent

    # -- Mutations -----------------------------------------------------------

    def reload(self) -> None:
        """Re-read the on-disk snapshot, discarding the cached one.

        ``__init__`` reads the balance once and every mutation works from that
        cached copy, which is correct only while this process is the wallet's
        sole writer. It is not: skharness settles the same files from its own
        interpreter, and so does every other session on the box. A caller that
        holds :func:`settle_lock` must call this immediately after taking the
        lock, because the value it is about to modify may have been written by
        somebody else since construction. Reloading OUTSIDE the lock buys
        nothing: the refreshed balance can go stale again before the write.

        Raises:
            WalletCorruptionError: the on-disk snapshot exists but cannot be
                parsed. Propagated deliberately, exactly as at construction.
        """
        with self._lock:
            self._snapshot = self._load_or_create_snapshot()

    def mint(
        self,
        amount: int,
        description: str = "",
        proof_hash: str = "",
    ) -> Transaction:
        """Mint new Joules into this wallet.

        Args:
            amount: Joules to mint (must be > 0).
            description: Why the Joules are being minted.
            proof_hash: Hash of the proof artifact.

        Returns:
            The Transaction record created.

        Raises:
            ValueError: If amount is not positive.
        """
        if amount <= 0:
            raise ValueError(f"Mint amount must be positive, got {amount}")
        with self._lock:
            self._snapshot.balance += amount
            self._snapshot.total_minted += amount
            txn = Transaction(
                kind=TransactionKind.MINT,
                amount=amount,
                counterparty="economy",
                description=description,
                proof_hash=proof_hash,
                balance_after=self._snapshot.balance,
            )
            self._persist(txn)
            return txn

    def spend(
        self,
        amount: int,
        description: str = "",
        proof_hash: str = "",
    ) -> Transaction:
        """Spend Joules from this wallet.

        Args:
            amount: Joules to spend (must be > 0).
            description: What the spend is for.
            proof_hash: Hash of the proof artifact.

        Returns:
            The Transaction record created.

        Raises:
            ValueError: If amount is not positive or exceeds balance.
        """
        if amount <= 0:
            raise ValueError(f"Spend amount must be positive, got {amount}")
        with self._lock:
            if amount > self._snapshot.balance:
                raise ValueError(
                    f"Insufficient balance: need {amount}J, have {self._snapshot.balance}J"
                )
            self._snapshot.balance -= amount
            self._snapshot.total_spent += amount
            txn = Transaction(
                kind=TransactionKind.SPEND,
                amount=amount,
                counterparty="economy",
                description=description,
                proof_hash=proof_hash,
                balance_after=self._snapshot.balance,
            )
            self._persist(txn)
            return txn

    def transfer(
        self,
        target_wallet: "JouleWallet",
        amount: int,
        description: str = "",
    ) -> tuple[Transaction, Transaction]:
        """Transfer Joules from this wallet to another.

        Acquires locks on both wallets in a consistent order (by agent
        name) to avoid deadlocks.

        Args:
            target_wallet: Destination wallet.
            amount: Joules to transfer.
            description: Reason for transfer.

        Returns:
            Tuple of (sender_txn, receiver_txn).

        Raises:
            ValueError: If amount is invalid or balance insufficient.
        """
        if amount <= 0:
            raise ValueError(f"Transfer amount must be positive, got {amount}")
        if target_wallet.agent == self._agent:
            raise ValueError("Cannot transfer to self")

        # Consistent lock ordering to prevent deadlocks
        first, second = sorted([self, target_wallet], key=lambda w: w.agent)
        with first._lock:
            with second._lock:
                if amount > self._snapshot.balance:
                    raise ValueError(
                        f"Insufficient balance: need {amount}J, have {self._snapshot.balance}J"
                    )

                # Debit sender
                self._snapshot.balance -= amount
                self._snapshot.total_transferred_out += amount
                send_txn = Transaction(
                    kind=TransactionKind.TRANSFER_OUT,
                    amount=amount,
                    counterparty=target_wallet.agent,
                    description=description,
                    balance_after=self._snapshot.balance,
                )
                self._persist_unlocked(send_txn)

                # Credit receiver
                target_wallet._snapshot.balance += amount
                target_wallet._snapshot.total_transferred_in += amount
                recv_txn = Transaction(
                    kind=TransactionKind.TRANSFER_IN,
                    amount=amount,
                    counterparty=self._agent,
                    description=description,
                    balance_after=target_wallet._snapshot.balance,
                )
                target_wallet._persist_unlocked(recv_txn)

                return send_txn, recv_txn

    # -- Read operations -----------------------------------------------------

    def get_transactions(self, limit: int = 50) -> list[Transaction]:
        """Read the most recent transactions from the log.

        Args:
            limit: Maximum number of transactions to return.

        Returns:
            List of Transaction objects, most recent first.
        """
        with self._lock:
            return self._read_log(limit)

    def get_pl_statement(self, period: str = "all-time") -> PLStatement:
        """Generate a P&L statement for this wallet.

        Args:
            period: Human-readable label for the reporting period.

        Returns:
            PLStatement with earnings, costs, and net position.
        """
        llm_cost = self._get_llm_cost_usd()
        with self._lock:
            snap = self._snapshot
            net = (
                snap.total_minted
                + snap.total_transferred_in
                - snap.total_spent
                - snap.total_transferred_out
            )
            return PLStatement(
                agent=self._agent,
                period=period,
                joules_earned=snap.total_minted,
                joules_spent=snap.total_spent,
                joules_transferred_in=snap.total_transferred_in,
                joules_transferred_out=snap.total_transferred_out,
                net_joules=net,
                llm_cost_usd=llm_cost,
                current_balance=snap.balance,
            )

    # -- Persistence ---------------------------------------------------------

    def _load_or_create_snapshot(self) -> WalletSnapshot:
        """Load wallet state from disk, or create and persist a fresh one.

        Ensures the wallet directory exists (parents=True, exist_ok=True)
        and writes an initial joules.json if none is found, so the file
        is always present on disk after construction.

        A snapshot file that exists but is empty, unreadable, or fails to
        parse is treated as corruption, not as an absent wallet: it raises
        WalletCorruptionError rather than silently returning a fresh
        zero-balance snapshot. Only a genuinely missing file (a wallet
        that has never been created) gets a real zero balance.
        """
        self._wallet_dir.mkdir(parents=True, exist_ok=True)
        if self._state_path.exists():
            try:
                raw = self._state_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise WalletCorruptionError(
                    self._agent, self._state_path, self._log_path, f"unreadable: {exc}"
                ) from exc
            if not raw.strip():
                raise WalletCorruptionError(
                    self._agent, self._state_path, self._log_path, "snapshot file is empty"
                )
            try:
                data = json.loads(raw)
                return WalletSnapshot(**data)
            except (json.JSONDecodeError, ValueError) as exc:
                raise WalletCorruptionError(
                    self._agent, self._state_path, self._log_path, str(exc)
                ) from exc
        snapshot = WalletSnapshot(agent=self._agent)
        # Persist the fresh snapshot so joules.json exists on disk immediately
        try:
            atomic_write_text(self._state_path, json.dumps(snapshot.model_dump(), indent=2))
        except OSError as exc:
            logger.error("Failed to initialize wallet for %s: %s", self._agent, exc)
        return snapshot

    def _persist(self, txn: Transaction) -> None:
        """Save snapshot and append transaction (caller must hold lock)."""
        self._persist_unlocked(txn)

    def _persist_unlocked(self, txn: Transaction) -> None:
        """Save snapshot and append transaction (no lock assumed).

        This is the raw persistence call used by both _persist() and
        the transfer() method which manages its own locking.

        JOURNAL FIRST, THEN STATE. The order matters and it is the whole
        point of this method.

        These are two separate writes and they cannot be made atomic with
        each other on a plain filesystem, so the only question is which one
        survives a failure. Writing the snapshot first (the previous order)
        meant a failed or interrupted log append left the balance advanced
        with no transaction explaining it, and that is unrecoverable: the
        amount is gone, so no replay can ever reconstruct it. Writing the
        journal first means a failure leaves a recorded transaction whose
        snapshot has not caught up, which `replay_balance()` repairs exactly.

        A log-append failure therefore RAISES rather than being logged and
        swallowed. An unjournaled balance change is worse than a failed
        transaction: the caller can retry a failure, but nobody can
        reconstruct a mutation that was never written down.

        The snapshot write stays atomic (temp file in the same directory,
        fsync, os.replace), so a crash mid-write leaves either the whole old
        snapshot or the whole new one, never a truncated file.

        Observed 2026-08-16, which is why this changed: 3 of 19 live wallets
        carried exactly one break point each where balance_after ran ahead of
        the summed amounts and then propagated that offset forever, and 2 more
        held a balance with no transaction log at all.

        Raises:
            OSError: the transaction could not be journaled. The snapshot is
                left untouched, so the wallet keeps its last consistent state.
        """
        self._snapshot.updated_at = datetime.now(timezone.utc).isoformat()
        self._wallet_dir.mkdir(parents=True, exist_ok=True)

        # 1. Journal. Durable and fsynced before any balance is published.
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(txn.model_dump()) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            logger.error(
                "Failed to journal transaction for %s, snapshot NOT advanced: %s",
                self._agent,
                exc,
            )
            # Callers mutate the in-memory snapshot before persisting, so that
            # mutation is now ahead of both the journal and the disk state.
            # Reload the last consistent snapshot so a caller that catches this
            # does not keep spending against a balance that was never recorded.
            try:
                self._snapshot = self._load_or_create_snapshot()
            except Exception:  # noqa: BLE001 - never mask the original OSError
                logger.exception("Could not restore wallet snapshot for %s", self._agent)
            raise

        # 2. State. Safe to lose: replay_balance() rebuilds it from the journal.
        try:
            atomic_write_text(self._state_path, json.dumps(self._snapshot.model_dump(), indent=2))
        except OSError as exc:
            logger.error("Failed to write wallet state for %s: %s", self._agent, exc)

    def _read_log(self, limit: int) -> list[Transaction]:
        """Read the last N transactions from the JSONL log."""
        if not self._log_path.exists():
            return []
        try:
            lines = self._log_path.read_text(encoding="utf-8").strip().splitlines()
            recent = lines[-limit:] if limit < len(lines) else lines
            txns = []
            for line in reversed(recent):
                line = line.strip()
                if line:
                    try:
                        txns.append(Transaction(**json.loads(line)))
                    except (json.JSONDecodeError, ValueError):
                        continue
            return txns
        except OSError as exc:
            logger.warning("Failed to read transaction log for %s: %s", self._agent, exc)
            return []

    def _get_llm_cost_usd(self) -> float:
        """Pull aggregate LLM cost from the usage tracker.

        Returns 0.0 if usage data is unavailable.
        """
        try:
            from .usage import UsageTracker

            agent_home = Path(SHARED_ROOT).expanduser() / "agents" / self._agent
            # Fall back to the shared home if agent-specific usage dir doesn't exist
            usage_home = (
                agent_home if (agent_home / "usage").exists() else Path(AGENT_HOME).expanduser()
            )
            tracker = UsageTracker(home=usage_home)
            reports = tracker.get_monthly()
            agg = tracker.aggregate(reports)
            return agg.total_cost_usd
        except Exception as exc:
            logger.debug("Could not fetch LLM cost for %s: %s", self._agent, exc)
            return 0.0


# ---------------------------------------------------------------------------
# Ledger replay and audit -- read-only verification primitives
# ---------------------------------------------------------------------------


def replay_balance(agent_name: str, home: Optional[Path] = None) -> int:
    """Recompute a wallet's balance purely by replaying transactions.jsonl.

    This never reads or writes joules.json, and never writes anything at
    all. It is the verification primitive for confirming a snapshot
    matches its ledger, and the recovery primitive for rebuilding a
    balance when a snapshot is corrupt, stale, or missing. It works even
    when the wallet's snapshot is corrupt enough that constructing a
    JouleWallet would raise WalletCorruptionError, since it never touches
    the snapshot file at all.

    Malformed lines in the ledger are skipped, matching the tolerance
    already used by JouleWallet.get_transactions().

    Any disagreement between this and the snapshot balance is a
    pre-existing corruption to surface, never something for this
    function (or anything downstream of it) to auto-correct.

    Args:
        agent_name: The agent whose ledger to replay.
        home: Root skcapstone directory (default from SHARED_ROOT).

    Returns:
        The balance computed by folding every transaction in file order.
        Zero if the agent has no transaction log yet.
    """
    root = Path(home) if home else Path(SHARED_ROOT).expanduser()
    log_path = root / "agents" / agent_name / "wallet" / "transactions.jsonl"
    if not log_path.exists():
        return 0

    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read transaction log for %s: %s", agent_name, exc)
        return 0

    balance = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = entry.get("kind")
        amount = entry.get("amount", 0)
        if kind in (TransactionKind.MINT.value, TransactionKind.TRANSFER_IN.value):
            balance += amount
        elif kind in (TransactionKind.SPEND.value, TransactionKind.TRANSFER_OUT.value):
            balance -= amount
    return balance


class WalletAuditResult(BaseModel):
    """Snapshot-vs-ledger comparison for a single wallet.

    Produced by audit_wallets(). Nothing that produces this result
    writes to a wallet, repairs a wallet, or resets a wallet.
    """

    agent: str
    snapshot_balance: Optional[int] = Field(
        default=None, description="Balance in joules.json, or None if unreadable"
    )
    replayed_balance: int = Field(
        default=0, description="Balance recomputed from transactions.jsonl"
    )
    agrees: bool = Field(default=False, description="True if snapshot_balance == replayed_balance")
    error: str = Field(default="", description="Why the snapshot could not be compared, if any")


def reconcile_wallet(
    agent_name: str,
    home: Optional[Path] = None,
    *,
    note: str = "",
    dry_run: bool = True,
) -> dict:
    """Make a wallet's journal agree with its authoritative snapshot.

    Chef's ruling 2026-08-16: where the two disagree, THE SNAPSHOT WINS.
    Those balances are what every consumer has read and acted on since March,
    and the journal is the side proven unreliable (for `opus` the snapshot and
    the log's own last `balance_after` agree with each other; only the sum of
    amounts dissents). Rebuilding balances from a journal just shown to be
    lossy would be backwards.

    The correction is WRITTEN DOWN, never applied silently. The whole defect
    was a balance that could not be explained by its own history, so the repair
    has to be a journal entry that says what changed and why. After this runs,
    `replay_balance() == snapshot` and the two stay in agreement, because
    `_persist_unlocked()` now journals before it publishes state.

    Critically this appends to the journal WITHOUT moving the snapshot: using
    mint()/spend() would advance the balance too and re-open the same gap it is
    closing. The entry carries `balance_after` equal to the unchanged snapshot.

    Args:
        agent_name: Wallet to reconcile.
        home: Agent home root; defaults to the live one.
        note: Extra context recorded in the transaction description.
        dry_run: When True (default) compute and report, write nothing.

    Returns:
        A dict describing the wallet, both balances, the delta, and whether a
        correcting entry was written.
    """
    wallet = JouleWallet(agent_name, home=home)
    snapshot = int(wallet.balance)
    replayed = int(replay_balance(agent_name, home=home))
    delta = snapshot - replayed

    result = {
        "agent": agent_name,
        "snapshot_balance": snapshot,
        "replayed_balance": replayed,
        "delta": delta,
        "written": False,
        "kind": None,
    }
    if delta == 0:
        return result

    # delta > 0: the journal under-counts, so credit it up to the snapshot.
    # delta < 0: the journal over-counts, so debit it down.
    kind = TransactionKind.MINT if delta > 0 else TransactionKind.SPEND
    result["kind"] = kind.value
    if dry_run:
        return result

    desc = (
        f"LEDGER RECONCILIATION: snapshot authoritative, journal adjusted by "
        f"{delta:+d}J to match. Closes a historical gap where balance_after ran "
        f"ahead of the summed amounts. Balance itself is UNCHANGED."
    )
    if note:
        desc = f"{desc} {note}"

    txn = Transaction(
        kind=kind,
        amount=abs(delta),
        counterparty="reconciliation",
        description=desc,
        proof_hash=(
            XPBridge.compute_proof_hash(f"reconcile:{agent_name}:{snapshot}:{replayed}")
            if "XPBridge" in globals()
            else ""
        ),
        balance_after=snapshot,
    )
    with wallet._log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(txn.model_dump()) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

    result["written"] = True
    return result


def audit_wallets(home: Optional[Path] = None) -> list[WalletAuditResult]:
    """Compare every agent's snapshot balance against a ledger replay.

    Read-only across the board: it never writes, never repairs, and
    never resets a wallet. Any snapshot that fails to parse is reported
    with an error string instead of raising, since surfacing exactly
    that disagreement for a human to look at is the entire purpose of
    this function.

    Args:
        home: Root skcapstone directory (default from SHARED_ROOT).

    Returns:
        One WalletAuditResult per agent directory that has a wallet
        directory, sorted by agent name.
    """
    root = Path(home) if home else Path(SHARED_ROOT).expanduser()
    agents_dir = root / "agents"
    results: list[WalletAuditResult] = []
    if not agents_dir.exists():
        return results

    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        wallet_dir = agent_dir / "wallet"
        state_path = wallet_dir / "joules.json"
        log_path = wallet_dir / "transactions.jsonl"
        if not state_path.exists() and not log_path.exists():
            continue

        agent_name = agent_dir.name
        replayed = replay_balance(agent_name, home=root)

        snapshot_balance: Optional[int] = None
        error = ""
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                snapshot_balance = WalletSnapshot(**data).balance
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                error = f"snapshot unreadable: {exc}"
        else:
            error = "no snapshot file, ledger only"

        agrees = snapshot_balance is not None and snapshot_balance == replayed
        results.append(
            WalletAuditResult(
                agent=agent_name,
                snapshot_balance=snapshot_balance,
                replayed_balance=replayed,
                agrees=agrees,
                error=error,
            )
        )

    return results


# ---------------------------------------------------------------------------
# XPBridge -- converts XP events to Joule amounts
# ---------------------------------------------------------------------------


# Base Joule rewards by XP event type
_XP_JOULE_TABLE: dict[str, int] = {
    "code_commit": 100,
    "bug_fix": 500,
    "documentation": 200,
    "task_complete": 25,  # base -- multiplied by priority and quality
    "sale_closed": 2000,
    "consulting_hour": 200,
    "code_review": 150,
    "test_written": 100,
    "deployment": 300,
    "incident_resolved": 750,
}

# Priority multipliers for task_complete events
_PRIORITY_MULTIPLIER: dict[str, float] = {
    "critical": 4.0,
    "high": 2.0,
    "medium": 1.0,
    "low": 0.5,
}

# Quality multipliers for task_complete events
_QUALITY_MULTIPLIER: dict[str, float] = {
    "excellent": 3.0,
    "good": 2.0,
    "acceptable": 1.0,
    "needs_improvement": 0.5,
}

# Category mapping from XP event types
_EVENT_CATEGORY: dict[str, WorkCategory] = {
    "code_commit": WorkCategory.DEVELOPMENT,
    "bug_fix": WorkCategory.DEVELOPMENT,
    "documentation": WorkCategory.DEVELOPMENT,
    "task_complete": WorkCategory.OPERATIONS,
    "sale_closed": WorkCategory.BUSINESS,
    "consulting_hour": WorkCategory.BUSINESS,
    "code_review": WorkCategory.DEVELOPMENT,
    "test_written": WorkCategory.DEVELOPMENT,
    "deployment": WorkCategory.OPERATIONS,
    "incident_resolved": WorkCategory.OPERATIONS,
}


class XPBridge:
    """Converts XP events into Joule minting amounts.

    The bridge applies base rewards from a lookup table, then scales
    task_complete events by priority and quality multipliers.

    Usage::

        bridge = XPBridge()
        joules = bridge.calculate_joules("code_commit")
        joules = bridge.calculate_joules(
            "task_complete", priority="high", quality="good"
        )
    """

    def __init__(
        self,
        joule_table: Optional[dict[str, int]] = None,
        priority_multipliers: Optional[dict[str, float]] = None,
        quality_multipliers: Optional[dict[str, float]] = None,
    ) -> None:
        self._joule_table = joule_table or dict(_XP_JOULE_TABLE)
        self._priority_mult = priority_multipliers or dict(_PRIORITY_MULTIPLIER)
        self._quality_mult = quality_multipliers or dict(_QUALITY_MULTIPLIER)

    def calculate_joules(
        self,
        event_type: str,
        priority: str = "medium",
        quality: str = "acceptable",
    ) -> int:
        """Calculate Joule reward for an XP event.

        Args:
            event_type: The type of work event (e.g. 'code_commit', 'task_complete').
            priority: Task priority level (only affects task_complete).
            quality: Quality assessment (only affects task_complete).

        Returns:
            Number of Joules to mint.
        """
        base = self._joule_table.get(event_type, 0)
        if base == 0:
            logger.debug("Unknown XP event type: %s", event_type)
            return 0

        if event_type == "task_complete":
            p_mult = self._priority_mult.get(priority, 1.0)
            q_mult = self._quality_mult.get(quality, 1.0)
            return max(1, int(base * p_mult * q_mult))

        return base

    def get_category(self, event_type: str) -> WorkCategory:
        """Map an XP event type to a WorkCategory.

        Args:
            event_type: The XP event type string.

        Returns:
            Appropriate WorkCategory, defaults to OPERATIONS.
        """
        return _EVENT_CATEGORY.get(event_type, WorkCategory.OPERATIONS)

    @staticmethod
    def compute_proof_hash(data: str) -> str:
        """Compute a SHA-256 proof hash for an artifact.

        Args:
            data: String content to hash (commit message, task JSON, etc.).

        Returns:
            Hex-encoded SHA-256 digest.
        """
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    @property
    def reward_table(self) -> dict[str, int]:
        """Return a copy of the current reward table."""
        return dict(self._joule_table)

    @property
    def priority_multipliers(self) -> dict[str, float]:
        """Return a copy of the priority multiplier table."""
        return dict(self._priority_mult)

    @property
    def quality_multipliers(self) -> dict[str, float]:
        """Return a copy of the quality multiplier table."""
        return dict(self._quality_mult)


# ---------------------------------------------------------------------------
# JouleEngine -- orchestrates the full economic flow
# ---------------------------------------------------------------------------


class JouleEngine:
    """Orchestrates Joule minting, spending, and reporting.

    The engine is the central coordinator: it takes work events,
    calculates rewards via the XPBridge, mints Joules into wallets,
    and provides P&L and network-wide reporting.

    Args:
        home: Root skcapstone directory.
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        self._home = Path(home) if home else Path(SHARED_ROOT).expanduser()
        self._bridge = XPBridge()
        self._wallets: dict[str, JouleWallet] = {}
        self._lock = threading.Lock()

    # -- Wallet management ---------------------------------------------------

    def get_wallet(self, agent_name: str) -> JouleWallet:
        """Get or create a wallet for an agent.

        Args:
            agent_name: The agent's name.

        Returns:
            The agent's JouleWallet instance.
        """
        with self._lock:
            if agent_name not in self._wallets:
                self._wallets[agent_name] = JouleWallet(agent_name, home=self._home)
            return self._wallets[agent_name]

    # -- Work recording ------------------------------------------------------

    def record_work(
        self,
        worker: str,
        category: WorkCategory | str,
        description: str,
        proof_hash: str = "",
        joules: Optional[int] = None,
        event_type: str = "task_complete",
        priority: str = "medium",
        quality: str = "acceptable",
    ) -> WorkRecord:
        """Record a unit of work and mint Joules into the worker's wallet.

        If ``joules`` is not specified, the amount is calculated from
        the ``event_type`` using the XPBridge.

        Args:
            worker: Agent or human name.
            category: Work category (string or WorkCategory enum).
            description: What was done.
            proof_hash: SHA-256 hash of proof artifact.
            joules: Explicit Joule amount (overrides XPBridge calculation).
            event_type: XP event type for automatic calculation.
            priority: Task priority (for task_complete events).
            quality: Quality level (for task_complete events).

        Returns:
            The WorkRecord that was created.
        """
        if isinstance(category, str):
            try:
                category = WorkCategory(category)
            except ValueError:
                logger.warning("Unknown category '%s', defaulting to operations", category)
                category = WorkCategory.OPERATIONS

        if joules is None:
            joules = self._bridge.calculate_joules(event_type, priority, quality)

        if not proof_hash:
            proof_data = f"{worker}:{category.value}:{description}:{time.time()}"
            proof_hash = XPBridge.compute_proof_hash(proof_data)

        record = WorkRecord(
            worker=worker,
            category=category,
            description=description,
            joules=joules,
            proof_hash=proof_hash,
        )

        # Mint into wallet.
        #
        # Everything that reads or writes the balance runs inside the lock, and
        # that INCLUDES getting the wallet, because the read this protects is
        # the snapshot load in JouleWallet.__init__ (or, for a wallet already in
        # this engine's cache, whenever that load last happened). Taking the
        # wallet outside and locking only the mutation would leave the identical
        # bug: two writers would still capture the same stale balance and the
        # second write would still erase the first.
        with settle_lock(worker, home=self._home):
            wallet = self.get_wallet(worker)
            wallet.reload()
            wallet.mint(
                amount=joules,
                description=description,
                proof_hash=proof_hash,
            )

        logger.info(
            "Recorded %dJ for %s (%s): %s",
            joules,
            worker,
            category.value,
            description,
        )
        return record

    def auto_tokenize_task(self, task_data: dict[str, Any]) -> Optional[WorkRecord]:
        """Calculate and mint Joules for a completed coordination task.

        Reads task fields from the coordination module's Task format
        and computes reward based on priority and tags.

        Args:
            task_data: Dict with at least 'title', and optionally
                       'priority', 'tags', 'created_by', 'id',
                       'description'.

        Returns:
            WorkRecord if minting succeeded, None if task data is invalid.
        """
        title = task_data.get("title", "")
        if not title:
            logger.warning("auto_tokenize_task called with empty title")
            return None

        worker = task_data.get("completed_by") or task_data.get("created_by", "unknown")
        priority = task_data.get("priority", "medium")
        tags = task_data.get("tags", [])
        task_id = task_data.get("id", "")
        task_data.get("description", "")

        # Infer quality from tags
        quality = "acceptable"
        if "excellent" in tags or "quality:excellent" in tags:
            quality = "excellent"
        elif "good" in tags or "quality:good" in tags:
            quality = "good"
        elif "needs_improvement" in tags or "quality:needs_improvement" in tags:
            quality = "needs_improvement"

        # Infer category from tags
        category = WorkCategory.OPERATIONS
        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower in ("dev", "development", "code", "engineering"):
                category = WorkCategory.DEVELOPMENT
                break
            elif tag_lower in ("biz", "business", "sales", "revenue"):
                category = WorkCategory.BUSINESS
                break
            elif tag_lower in ("community", "docs", "outreach"):
                category = WorkCategory.COMMUNITY
                break
            elif tag_lower in ("physical", "hardware", "infra"):
                category = WorkCategory.PHYSICAL
                break

        # Build proof hash from task data
        proof_data = json.dumps(task_data, sort_keys=True, default=str)
        proof_hash = XPBridge.compute_proof_hash(proof_data)

        joules = self._bridge.calculate_joules("task_complete", priority=priority, quality=quality)

        desc = f"Task completed: {title}"
        if task_id:
            desc = f"[{task_id}] {desc}"

        return self.record_work(
            worker=worker,
            category=category,
            description=desc,
            proof_hash=proof_hash,
            joules=joules,
            event_type="task_complete",
            priority=priority,
            quality=quality,
        )

    # -- Reporting -----------------------------------------------------------

    def get_agent_pl(self, agent_name: str) -> PLStatement:
        """Generate a P&L statement for an agent.

        Args:
            agent_name: The agent whose P&L to compute.

        Returns:
            PLStatement with earnings, costs, and net position.
        """
        wallet = self.get_wallet(agent_name)
        return wallet.get_pl_statement(period="last 30 days")

    def get_network_stats(self) -> NetworkStats:
        """Compute network-wide economic statistics.

        Scans all agent wallet directories under the shared root
        to aggregate totals.

        Returns:
            NetworkStats with totals across all agents.
        """
        agents_dir = self._home / "agents"
        stats = NetworkStats()

        if not agents_dir.exists():
            return stats

        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            wallet_file = agent_dir / "wallet" / "joules.json"
            if not wallet_file.exists():
                continue
            try:
                data = json.loads(wallet_file.read_text(encoding="utf-8"))
                snap = WalletSnapshot(**data)
                stats.total_minted += snap.total_minted
                stats.total_spent += snap.total_spent
                stats.total_transfers += snap.total_transferred_in + snap.total_transferred_out
                stats.agent_balances[snap.agent] = snap.balance
                if snap.balance > 0 or snap.total_minted > 0:
                    stats.active_agents += 1
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                logger.debug("Skipping wallet for %s: %s", agent_dir.name, exc)

        return stats

    @property
    def bridge(self) -> XPBridge:
        """Access the XPBridge for direct Joule calculations."""
        return self._bridge
