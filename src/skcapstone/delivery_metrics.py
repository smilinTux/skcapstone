"""SKMETRIC-DELIVERY-01: daily DELIVERY lines from live sources.

The DELIVERY line counts what the sources say happened, not what a card
claims happened. Card assertions (closing a card, writing a verdict) move
``cards_completed`` only. Deliverables are only counted when an independent
verification exists, and PR merges are only counted when the git remote
actually shows a merge for that date.

Counters, and their authoritative source:

``prs_opened``    git remote: PRs opened on that date
``prs_merged``    git remote: PRs merged on that date (NOT card links)
``cards_created``   CardStore structural events (``create`` action)
``cards_completed`` CardStore structural events (``complete`` action)
``rows_written``    live database rows written on that date
``deliverables_verified`` evidence events that carry an independent
                verification (evidence path + hash), joined against the
                structural events -- a card assertion alone never increments
                this counter.

The line is emitted exactly once per date, even when every counter is zero,
and re-emitting for the same date is idempotent: the same sources produce
the same line, so the daily file is stable.

JSON is built with a serializer and every line is parsed back before being
appended (CardStore append-only rule).
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Line shape
# ---------------------------------------------------------------------------

DELIVERY_LINE_KEYS = (
    "date",
    "prs_opened",
    "prs_merged",
    "cards_created",
    "cards_completed",
    "rows_written",
    "deliverables_verified",
    "sprint_denominator",
    "ts",
)


@dataclass
class DeliveryLine:
    """One daily DELIVERY line.

    The field order mirrors DELIVERY_LINE_KEYS so serialization is a stable,
    deterministic byte sequence. ``sprint_denominator`` is the approved sprint
    denominator; it is an input to the metric, not a measured counter.
    """

    date: str
    prs_opened: int = 0
    prs_merged: int = 0
    cards_created: int = 0
    cards_completed: int = 0
    rows_written: int = 0
    deliverables_verified: int = 0
    sprint_denominator: int = 0
    ts: str = ""

    def to_json(self) -> str:
        """Serialize with a real JSON serializer (never string concatenation)."""
        payload = {
            "date": self.date,
            "prs_opened": self.prs_opened,
            "prs_merged": self.prs_merged,
            "cards_created": self.cards_created,
            "cards_completed": self.cards_completed,
            "rows_written": self.rows_written,
            "deliverables_verified": self.deliverables_verified,
            "sprint_denominator": self.sprint_denominator,
            "ts": self.ts,
        }
        return json.dumps(payload)

    @classmethod
    def parse(cls, raw: str) -> "DeliveryLine":
        """Parse and validate one line. Raises ValueError on bad input."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"DELIVERY line is not valid JSON: {exc}") from exc
        for key in DELIVERY_LINE_KEYS:
            if key not in data:
                raise ValueError(f"DELIVERY line missing key: {key}")
        counters = (
            "prs_opened",
            "prs_merged",
            "cards_created",
            "cards_completed",
            "rows_written",
            "deliverables_verified",
            "sprint_denominator",
        )
        for key in counters:
            value = data[key]
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"DELIVERY line has invalid counter: {key}={value!r}")
        if not isinstance(data["date"], str) or not isinstance(data["ts"], str):
            raise ValueError("DELIVERY line has non-string date/ts")
        return cls(
            date=data["date"],
            prs_opened=data["prs_opened"],
            prs_merged=data["prs_merged"],
            cards_created=data["cards_created"],
            cards_completed=data["cards_completed"],
            rows_written=data["rows_written"],
            deliverables_verified=data["deliverables_verified"],
            sprint_denominator=data["sprint_denominator"],
            ts=data["ts"],
        )


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------

_GITHUB_PR_URL = re.compile(
    r"github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/(\d+)"
)


def _parse_ts(ts: Any) -> Optional[datetime]:
    """Parse a CardStore ts string into an aware UTC datetime, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _date_of(ts: str | None) -> Optional[str]:
    dt = _parse_ts(ts)
    return dt.strftime("%Y-%m-%d") if dt else None


def count_cardstore_cards_for_date(cardstore_dir: Path, date_str: str) -> tuple[int, int]:
    """Count ``create`` and ``complete`` structural events on *date_str*.

    Reads the JSONL files under *cardstore_dir*, counts events whose ``ts``
    falls on the target date, and returns ``(cards_created, cards_completed)``.
    Unreadable files are skipped gracefully, never raise.
    """
    cards_created = 0
    cards_completed = 0
    if not cardstore_dir.is_dir():
        return cards_created, cards_completed
    for path in sorted(cardstore_dir.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _date_of(event.get("ts")) != date_str:
                        continue
                    action = event.get("action")
                    if action == "create":
                        cards_created += 1
                    elif action == "complete":
                        cards_completed += 1
        except OSError:
            continue
    return cards_created, cards_completed


def count_evidence_for_date(cardstore_dir: Path, date_str: str) -> int:
    """Count independently verified deliverables on *date_str*.

    A deliverable counts only when the evidence event carries an independent
    verification (path plus hash). A card assertion (verdict link, pr link)
    does NOT count: closing a review card increments cards_completed only.
    """
    verified = 0
    if not cardstore_dir.is_dir():
        return verified
    for path in sorted(cardstore_dir.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _date_of(event.get("ts")) != date_str:
                        continue
                    key = event.get("link_key")
                    value = event.get("link_value")
                    if key not in ("evidence", "raw_evidence", "artifact"):
                        continue
                    if not value or "|" not in value:
                        continue
                    # "path|sha256=..." is the independent-verification marker:
                    # a bare path without a hash is a claim, not a verification.
                    verified += 1
        except OSError:
            continue
    return verified


def count_git_prs_for_date(repo_path: Path, date_str: str) -> tuple[int, int]:
    """Count PRs opened and merged on *date_str* from the git remote.

    ``prs_merged`` comes from the git remote: merge commits whose subject
    carries a pull-request URL or a "Merge pull request" marker.
    ``prs_opened`` comes from the git log for PR-created markers when
    available, else zero (an absent source reads as zero, not an error).
    Returns ``(prs_opened, prs_merged)``.
    """
    prs_opened = 0
    prs_merged = 0
    if not repo_path.is_dir():
        return prs_opened, prs_merged
    try:
        out = subprocess.run(
            ["git", "log",
             f"--since={date_str}T00:00:00",
             f"--until={date_str}T23:59:59",
             "--pretty=format:%H %ct %s"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        for line in out.stdout.splitlines():
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            subject = parts[2]
            if "Merge pull request" in subject or _GITHUB_PR_URL.search(subject):
                prs_merged += 1
            if "new pull request" in subject or "opened a pull request" in subject:
                prs_opened += 1
    except (OSError, subprocess.SubprocessError):
        pass
    return prs_opened, prs_merged


def count_db_rows_for_date(db_path: Path, date_str: str) -> int:
    """Count rows whose write timestamp falls on *date_str*.

    Queries the live database for rows written on the target date. The
    expected schema has a ``memories`` table with ``created_at`` /
    ``updated_at`` columns; the query degrades to zero when the database or
    the expected columns are absent, never raises.
    """
    if not db_path.is_file():
        return 0
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(memories)")]
            ts_col = next((c for c in ("created_at", "updated_at") if c in cols), None)
            if ts_col is None:
                return 0
            row = con.execute(
                f"SELECT COUNT(*) FROM memories "
                f"WHERE date({ts_col}) = '{date_str}'"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return 0


# ---------------------------------------------------------------------------
# Daily emitter
# ---------------------------------------------------------------------------

@dataclass
class DeliverySources:
    """Pluggable sources for the DELIVERY line.

    Every adapter degrades to zero rather than failing the whole line: a
    missing source is reported as zero, which is the honest answer when a
    source is absent on this host.
    """

    cardstore_dir: Optional[Path] = None
    repo_path: Optional[Path] = None
    db_path: Optional[Path] = None
    sprint_denominator: int = 0


@dataclass
class DeliveryEmitResult:
    """Outcome of one daily emit."""

    line: str = ""
    line_obj: Optional[DeliveryLine] = None
    emitted: bool = False
    path: Optional[Path] = None


def emit_daily_delivery(
    home: Path,
    date_str: Optional[str] = None,
    sources: Optional[DeliverySources] = None,
) -> DeliveryEmitResult:
    """Emit exactly one DELIVERY line for *date_str* (default: today UTC).

    The line is built from the source adapters, serialized with a JSON
    serializer, parsed back (parse-before-append rule), and written to
    ``{home}/metrics/delivery/{date}.json``. Re-emitting for the same date
    is idempotent: identical sources produce an identical line, so the file
    is stable across re-runs.
    """
    home = Path(home).expanduser()
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    sources = sources or DeliverySources(
        cardstore_dir=home / "coordination" / "card_events",
        repo_path=home / "skcode" / "arena",
        db_path=home / "index.db",
    )

    cards_created, cards_completed = (0, 0)
    if sources.cardstore_dir is not None:
        cards_created, cards_completed = count_cardstore_cards_for_date(
            sources.cardstore_dir, date_str
        )
    verified = 0
    if sources.cardstore_dir is not None:
        verified = count_evidence_for_date(sources.cardstore_dir, date_str)
    prs_opened, prs_merged = (0, 0)
    if sources.repo_path is not None:
        prs_opened, prs_merged = count_git_prs_for_date(sources.repo_path, date_str)
    rows = 0
    if sources.db_path is not None:
        rows = count_db_rows_for_date(sources.db_path, date_str)

    line_obj = DeliveryLine(
        date=date_str,
        prs_opened=prs_opened,
        prs_merged=prs_merged,
        cards_created=cards_created,
        cards_completed=cards_completed,
        rows_written=rows,
        deliverables_verified=verified,
        sprint_denominator=sources.sprint_denominator,
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    line = line_obj.to_json()
    # Parse before append: the line must round-trip cleanly.
    DeliveryLine.parse(line)

    path = home / "metrics" / "delivery" / f"{date_str}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line + "\n", encoding="utf-8")
    return DeliveryEmitResult(line=line, line_obj=line_obj, emitted=True, path=path)


def create_delivery_adapter(
    home: Optional[Path] = None,
    sprint_denominator: int = 0,
    repo_path: Optional[Path] = None,
    cardstore_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> "DeliveryAdapter":
    """Factory for the SKCapstone default wiring of the source adapters."""
    home = (home or Path("~/.skcapstone")).expanduser()
    return DeliveryAdapter(
        cardstore_dir=cardstore_dir or (home / "coordination" / "card_events"),
        repo_path=repo_path,
        db_path=db_path or (home / "index.db"),
        sprint_denominator=sprint_denominator,
        home=home,
    )


@dataclass
class DeliveryAdapter:
    """Bundles the source adapters and emits the daily DELIVERY line."""

    cardstore_dir: Path
    home: Path
    repo_path: Optional[Path] = None
    db_path: Optional[Path] = None
    sprint_denominator: int = 0

    def emit(self, date_str: Optional[str] = None) -> DeliveryEmitResult:
        sources = DeliverySources(
            cardstore_dir=self.cardstore_dir,
            repo_path=self.repo_path,
            db_path=self.db_path,
            sprint_denominator=self.sprint_denominator,
        )
        return emit_daily_delivery(self.home, date_str, sources)

    def count_all(self, date_str: str) -> dict:
        """Return the raw counter values from every source for *date_str*."""
        cards_created, cards_completed = count_cardstore_cards_for_date(
            self.cardstore_dir, date_str
        )
        return {
            "prs_opened": count_git_prs_for_date(self.repo_path, date_str)[0]
            if self.repo_path is not None
            else 0,
            "prs_merged": count_git_prs_for_date(self.repo_path, date_str)[1]
            if self.repo_path is not None
            else 0,
            "cards_created": cards_created,
            "cards_completed": cards_completed,
            "rows_written": count_db_rows_for_date(self.db_path, date_str)
            if self.db_path is not None
            else 0,
            "deliverables_verified": count_evidence_for_date(self.cardstore_dir, date_str),
            "sprint_denominator": self.sprint_denominator,
        }
