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

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import AGENT_HOME, SHARED_ROOT

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
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    balance_after: int = Field(
        default=0, description="Wallet balance after this transaction"
    )


class WalletSnapshot(BaseModel):
    """Serializable wallet state for persistence."""

    agent: str
    balance: int = 0
    total_minted: int = 0
    total_spent: int = 0
    total_transferred_in: int = 0
    total_transferred_out: int = 0
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


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
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class NetworkStats(BaseModel):
    """Aggregate stats across all agents in the economy."""

    total_minted: int = 0
    total_spent: int = 0
    total_transfers: int = 0
    active_agents: int = 0
    agent_balances: dict[str, int] = Field(default_factory=dict)
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


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
        self._wallet_dir = root / "agents" / agent_name / "wallet"
        self._state_path = self._wallet_dir / "joules.json"
        self._log_path = self._wallet_dir / "transactions.jsonl"
        self._lock = threading.Lock()
        self._snapshot = self._load_snapshot()

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
        first, second = sorted(
            [self, target_wallet], key=lambda w: w.agent
        )
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

    def _load_snapshot(self) -> WalletSnapshot:
        """Load wallet state from disk, or create a fresh one."""
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                return WalletSnapshot(**data)
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                logger.warning("Failed to load wallet for %s: %s", self._agent, exc)
        return WalletSnapshot(agent=self._agent)

    def _persist(self, txn: Transaction) -> None:
        """Save snapshot and append transaction (caller must hold lock)."""
        self._persist_unlocked(txn)

    def _persist_unlocked(self, txn: Transaction) -> None:
        """Save snapshot and append transaction (no lock assumed).

        This is the raw persistence call used by both _persist() and
        the transfer() method which manages its own locking.
        """
        self._snapshot.updated_at = datetime.now(timezone.utc).isoformat()
        self._wallet_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._state_path.write_text(
                json.dumps(self._snapshot.model_dump(), indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Failed to write wallet state for %s: %s", self._agent, exc)

        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(txn.model_dump()) + "\n")
        except OSError as exc:
            logger.error("Failed to append transaction for %s: %s", self._agent, exc)

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
            usage_home = agent_home if (agent_home / "usage").exists() else Path(AGENT_HOME).expanduser()
            tracker = UsageTracker(home=usage_home)
            reports = tracker.get_monthly()
            agg = tracker.aggregate(reports)
            return agg.total_cost_usd
        except Exception as exc:
            logger.debug("Could not fetch LLM cost for %s: %s", self._agent, exc)
            return 0.0


# ---------------------------------------------------------------------------
# XPBridge -- converts XP events to Joule amounts
# ---------------------------------------------------------------------------


# Base Joule rewards by XP event type
_XP_JOULE_TABLE: dict[str, int] = {
    "code_commit": 100,
    "bug_fix": 500,
    "documentation": 200,
    "task_complete": 25,      # base -- multiplied by priority and quality
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
                self._wallets[agent_name] = JouleWallet(
                    agent_name, home=self._home
                )
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

        # Mint into wallet
        wallet = self.get_wallet(worker)
        wallet.mint(
            amount=joules,
            description=description,
            proof_hash=proof_hash,
        )

        logger.info(
            "Recorded %dJ for %s (%s): %s",
            joules, worker, category.value, description,
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
        description_text = task_data.get("description", "")

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

        joules = self._bridge.calculate_joules(
            "task_complete", priority=priority, quality=quality
        )

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
                stats.total_transfers += (
                    snap.total_transferred_in + snap.total_transferred_out
                )
                stats.agent_balances[snap.agent] = snap.balance
                if snap.balance > 0 or snap.total_minted > 0:
                    stats.active_agents += 1
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                logger.debug(
                    "Skipping wallet for %s: %s", agent_dir.name, exc
                )

        return stats

    @property
    def bridge(self) -> XPBridge:
        """Access the XPBridge for direct Joule calculations."""
        return self._bridge
