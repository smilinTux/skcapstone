"""
Task auction system for multi-agent coordination.

When coord_create is called:
  1. An auction is opened and published to coord.auction
  2. Running agents auto-bid with their load metrics
     (claimed_tasks_count, cpu_percent)
  3. After AUCTION_WINDOW_SECS (5s), the lowest-load bidder
     auto-wins via coord_claim

Prevents duplicate claiming: coord_claim is blocked while an
auction is pending. All agents must go through the auction.

Auction state persists in:
    ~/.skcapstone/coordination/auctions/{task_id}.json

Topics used:
    coord.auction         — broadcast (auction_open / auction_resolved)
    coord.auction.bids.*  — per-task bid topic (agent → bid payload)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    pass

logger = logging.getLogger("skcapstone.auction")

AUCTION_TOPIC = "coord.auction"
BID_TOPIC_PREFIX = "coord.auction.bids"
AUCTION_WINDOW_SECS = 5


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AuctionBid(BaseModel):
    """A bid submitted by an agent during a task auction."""

    task_id: str
    agent: str
    claimed_tasks_count: int = 0
    cpu_percent: float = 0.0
    bid_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class AuctionRecord(BaseModel):
    """Persisted state of a single task auction."""

    task_id: str
    task_title: str
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    resolved_at: Optional[str] = None
    winner: Optional[str] = None
    bids: list[AuctionBid] = Field(default_factory=list)
    # pending | resolved | no_bidders
    status: str = "pending"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_score(bid: AuctionBid) -> float:
    """Lower score = less loaded = better bidder.

    Weighted formula: 70 % CPU utilisation, 30 % claimed-tasks ratio
    (capped at 20 tasks = 100 %).
    """
    cpu = min(bid.cpu_percent, 100.0) / 100.0
    tasks = min(bid.claimed_tasks_count, 20) / 20.0
    return 0.7 * cpu + 0.3 * tasks


def _collect_local_bid(task_id: str, agent_name: str, shared_root: Path) -> AuctionBid:
    """Build a bid from the local agent's live metrics."""
    # CPU usage
    try:
        import psutil  # optional dep

        cpu = psutil.cpu_percent(interval=0.1)
    except Exception:
        cpu = 0.0

    # Claimed-tasks count from agent file
    claimed_count = 0
    try:
        from .coordination import Board

        board = Board(shared_root)
        agent_file = board.load_agent(agent_name)
        if agent_file:
            claimed_count = len(agent_file.claimed_tasks)
    except Exception as exc:
        logger.warning("Failed to read claimed task count for agent %s: %s", agent_name, exc)

    return AuctionBid(
        task_id=task_id,
        agent=agent_name,
        claimed_tasks_count=claimed_count,
        cpu_percent=cpu,
    )


# ---------------------------------------------------------------------------
# AuctionManager
# ---------------------------------------------------------------------------


class AuctionManager:
    """Manages task auctions in the skcapstone coordination system.

    State is file-based and Syncthing-compatible — each auction is a
    single JSON file under coordination/auctions/.  Multiple agent
    processes on different machines will all write bids into the same
    file (via Syncthing sync); resolution reads the accumulated bids.

    Args:
        shared_root: Path to the shared skcapstone root
                     (the directory that contains coordination/).
    """

    def __init__(self, shared_root: Path) -> None:
        self.shared_root = Path(shared_root)
        self.auctions_dir = self.shared_root / "coordination" / "auctions"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self.auctions_dir.mkdir(parents=True, exist_ok=True)

    def _auction_path(self, task_id: str) -> Path:
        return self.auctions_dir / f"{task_id}.json"

    def _load_record(self, task_id: str) -> Optional[AuctionRecord]:
        path = self._auction_path(task_id)
        if not path.exists():
            return None
        try:
            return AuctionRecord.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            logger.warning("Failed to load auction record %s: %s", task_id, exc)
            return None

    def _save_record(self, record: AuctionRecord) -> None:
        self._ensure_dirs()
        self._auction_path(record.task_id).write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_auction(
        self, task_id: str, task_title: str, pubsub: Any
    ) -> AuctionRecord:
        """Open a new auction and broadcast it on coord.auction.

        Args:
            task_id: The task being auctioned.
            task_title: Human-readable title for the broadcast payload.
            pubsub: A PubSub instance used to publish the open notice.

        Returns:
            The newly created AuctionRecord.
        """
        record = AuctionRecord(task_id=task_id, task_title=task_title)
        self._save_record(record)

        try:
            pubsub.publish(
                AUCTION_TOPIC,
                {
                    "event": "auction_open",
                    "task_id": task_id,
                    "task_title": task_title,
                    "window_secs": AUCTION_WINDOW_SECS,
                    "bid_topic": f"{BID_TOPIC_PREFIX}.{task_id}",
                    "started_at": record.started_at,
                },
                ttl_seconds=60,
                tags=["auction", "open"],
            )
        except Exception as exc:
            logger.warning("Failed to publish auction_open for %s: %s", task_id, exc)

        logger.info("Auction opened for task %s (%s)", task_id, task_title)
        return record

    def submit_bid(self, bid: AuctionBid) -> bool:
        """Record a bid for a task auction.

        Deduplicates by agent: if the agent already bid, the new bid
        replaces the old one (agents may update their metrics).

        Args:
            bid: The bid to submit.

        Returns:
            True if the bid was accepted, False if the auction is
            closed or does not exist.
        """
        record = self._load_record(bid.task_id)
        if record is None or record.status != "pending":
            return False

        # Deduplicate by agent — keep the most recent bid
        record.bids = [b for b in record.bids if b.agent != bid.agent]
        record.bids.append(bid)
        self._save_record(record)
        logger.debug(
            "Bid accepted for %s from %s (cpu=%.1f%%, tasks=%d)",
            bid.task_id,
            bid.agent,
            bid.cpu_percent,
            bid.claimed_tasks_count,
        )
        return True

    def is_under_auction(self, task_id: str) -> bool:
        """Return True if there is an active (pending) auction for this task."""
        record = self._load_record(task_id)
        return record is not None and record.status == "pending"

    async def resolve_auction(
        self, task_id: str, pubsub: Any
    ) -> Optional[str]:
        """Wait for the bid window then assign the lowest-load bidder.

        Sleeps for AUCTION_WINDOW_SECS, then reads all accumulated bids
        from the auction record, picks the winner (lowest load score),
        and claims the task on their behalf via the Board.

        Args:
            task_id: The task being auctioned.
            pubsub: A PubSub instance used to publish the result.

        Returns:
            The winning agent name, or None if no bids were received.
        """
        await asyncio.sleep(AUCTION_WINDOW_SECS)

        record = self._load_record(task_id)
        if record is None:
            logger.warning("Auction record missing for %s at resolution time", task_id)
            return None
        if record.status != "pending":
            # Already resolved by another process (e.g. remote agent)
            return record.winner

        now_iso = datetime.now(timezone.utc).isoformat()

        if not record.bids:
            record.status = "no_bidders"
            record.resolved_at = now_iso
            self._save_record(record)
            logger.info("Auction %s: no bids received — task remains open", task_id)
            try:
                pubsub.publish(
                    AUCTION_TOPIC,
                    {"event": "auction_no_bidders", "task_id": task_id},
                    ttl_seconds=3600,
                    tags=["auction", "no_bidders"],
                )
            except Exception as exc:
                logger.warning("Failed to publish auction_no_bidders event for %s: %s", task_id, exc)
            return None

        winner_bid = min(record.bids, key=_load_score)
        winner = winner_bid.agent

        # Claim the task on behalf of the winner
        try:
            from .coordination import Board

            board = Board(self.shared_root)
            board.claim_task(winner, task_id)
            logger.info(
                "Auction %s: resolved — winner=%s (score=%.3f)",
                task_id,
                winner,
                _load_score(winner_bid),
            )
        except Exception as exc:
            logger.warning(
                "Auction %s: claim failed for %s: %s", task_id, winner, exc
            )
            # Still record the result even if claim failed
            winner = None

        record.winner = winner
        record.status = "resolved"
        record.resolved_at = now_iso
        self._save_record(record)

        try:
            pubsub.publish(
                AUCTION_TOPIC,
                {
                    "event": "auction_resolved",
                    "task_id": task_id,
                    "winner": winner,
                    "bids_count": len(record.bids),
                    "resolved_at": now_iso,
                },
                ttl_seconds=3600,
                tags=["auction", "resolved"],
            )
        except Exception as exc:
            logger.warning("Failed to publish auction_resolved event for %s: %s", task_id, exc)

        # Notify activity stream
        try:
            from . import activity

            activity.push(
                "task.auction_resolved",
                {"task_id": task_id, "winner": winner, "bids": len(record.bids)},
            )
        except Exception as exc:
            logger.warning("Failed to push auction_resolved activity for %s: %s", task_id, exc)

        return winner

    def get_stats(self, limit: int = 20) -> dict[str, Any]:
        """Return statistics on recent auctions.

        Args:
            limit: Maximum number of auction records to return (sorted by
                   most-recently modified).

        Returns:
            Dict with summary counts and per-auction detail.
        """
        self._ensure_dirs()
        records: list[AuctionRecord] = []
        try:
            paths = sorted(
                self.auctions_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]
        except Exception:
            paths = []

        for path in paths:
            try:
                records.append(
                    AuctionRecord.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except Exception:
                continue

        return {
            "total": len(records),
            "pending": sum(1 for r in records if r.status == "pending"),
            "resolved": sum(1 for r in records if r.status == "resolved"),
            "no_bidders": sum(1 for r in records if r.status == "no_bidders"),
            "auctions": [
                {
                    "task_id": r.task_id,
                    "task_title": r.task_title,
                    "status": r.status,
                    "winner": r.winner,
                    "bids_count": len(r.bids),
                    "started_at": r.started_at,
                    "resolved_at": r.resolved_at,
                    "bids": [
                        {
                            "agent": b.agent,
                            "claimed_tasks_count": b.claimed_tasks_count,
                            "cpu_percent": b.cpu_percent,
                            "score": round(_load_score(b), 3),
                        }
                        for b in sorted(r.bids, key=_load_score)
                    ],
                }
                for r in records
            ],
        }


# ---------------------------------------------------------------------------
# Background auto-bidder coroutine
# ---------------------------------------------------------------------------


async def run_auto_bidder(
    shared_root: Path,
    agent_name: str,
    poll_interval: float = 1.0,
) -> None:
    """Background coroutine: subscribe to coord.auction and auto-bid.

    Runs continuously (until cancelled).  Each iteration polls
    coord.auction for new auction_open messages and submits a bid
    with the local agent's live metrics.

    Args:
        shared_root: Path to the shared skcapstone root.
        agent_name: Name of the local agent.
        poll_interval: How often to poll for new auctions (seconds).
    """
    from .pubsub import PubSub

    ps = PubSub(shared_root, agent_name=agent_name)
    ps.subscribe(AUCTION_TOPIC)

    mgr = AuctionManager(shared_root)
    seen_task_ids: set[str] = set()

    # Initialise last_poll to now so we only pick up auctions opened
    # after this agent started — avoids re-bidding on stale messages.
    last_poll: Optional[datetime] = datetime.now(timezone.utc)

    logger.info("Auto-bidder started for agent '%s'", agent_name)

    while True:
        try:
            await asyncio.sleep(poll_interval)

            messages = ps.poll(AUCTION_TOPIC, since=last_poll, limit=50)
            if messages:
                last_poll = datetime.now(timezone.utc)

            for msg in messages:
                if msg.payload.get("event") != "auction_open":
                    continue

                task_id = msg.payload.get("task_id", "")
                if not task_id or task_id in seen_task_ids:
                    continue

                seen_task_ids.add(task_id)

                # Build and submit bid
                bid = _collect_local_bid(task_id, agent_name, shared_root)
                accepted = mgr.submit_bid(bid)
                if accepted:
                    logger.info(
                        "Auto-bid submitted for task %s: cpu=%.1f%% tasks=%d",
                        task_id,
                        bid.cpu_percent,
                        bid.claimed_tasks_count,
                    )
                    # Also publish bid to the per-task topic so remote
                    # agents running the resolver can see it
                    try:
                        ps.publish(
                            f"{BID_TOPIC_PREFIX}.{task_id}",
                            bid.model_dump(),
                            ttl_seconds=120,
                            tags=["bid", agent_name],
                        )
                    except Exception as exc:
                        logger.debug("Failed to publish bid to topic: %s", exc)

        except asyncio.CancelledError:
            logger.info("Auto-bidder cancelled for agent '%s'", agent_name)
            raise
        except Exception as exc:
            logger.warning("Auto-bidder error: %s", exc)
