"""GTD <-> skmemory time intentions (SKMEM-P2-05).

``gtd capture --due/--remind`` and the waiting-for sweep create skmemory time
intentions; ``gtd next`` / ``gtd waiting`` list the overdue ones.

skmemory is imported lazily, at call time, and never at module import: the
fleet pins ``skmemory==0.11.75`` (deploy/fleet-constraints.txt), which predates
intentions, and a pyproject floor must never exceed its pin. When the module is
absent every writer raises :class:`IntentionsUnavailable` (callers turn that into
a clear error that never loses the already-captured GTD item) and every reader
returns an empty list (the section is simply omitted).

Idempotency: each intention gets a deterministic id derived from the GTD item
id and the purpose, so repeating a capture or a sweep never makes a second one,
and a dismissed one (``done``) is not resurrected.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STALE_WAITING_DAYS = 14
_ACTIVE_STATES = ("pending", "armed")
_IN_RE = re.compile(
    r"^\s*in\s+(\d{1,6})\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\s*$", re.IGNORECASE
)
_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class IntentionsUnavailable(RuntimeError):
    """The installed skmemory has no intentions module."""


@dataclass
class _Api:
    Intention: Any
    open_store: Any  # () -> store with get/save/list/close


def _load_api() -> _Api | None:
    """Import skmemory intentions at runtime; None when unavailable."""
    try:
        from skmemory.backends.sqlite_backend import DEFAULT_BASE_PATH
        from skmemory.intentions import Intention, IntentionStore
    except Exception:  # noqa: BLE001 - old pin, missing package, broken import
        return None
    return _Api(Intention=Intention, open_store=lambda: IntentionStore(DEFAULT_BASE_PATH))


def available() -> bool:
    return _load_api() is not None


def _require() -> _Api:
    api = _load_api()
    if api is None:
        raise IntentionsUnavailable(
            "skmemory intentions are not available in this install (needs a skmemory "
            "newer than the pinned one); the GTD item was kept, no reminder was created"
        )
    return api


def parse_when(value: str, now: datetime | None = None) -> datetime:
    """Parse ``in N minutes|hours|days|weeks`` or an ISO 8601 time/date (naive = UTC)."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a time is required: an ISO 8601 date/time or 'in N days'")
    m = _IN_RE.match(value)
    if m:
        unit = _UNIT[m.group(2)[0].lower()]
        return now + timedelta(**{unit: int(m.group(1))})
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"unrecognised time {value!r}: use ISO 8601 (2026-12-01 or 2026-12-01T09:00:00Z) "
            "or 'in N days/hours/minutes/weeks'"
        ) from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def alert_channel() -> str:
    """Delivery channel named by ``$SKCAPSTONE_HOME/config/alert.env``, else ``inline``."""
    home = os.environ.get("SKCAPSTONE_HOME") or str(Path.home() / ".skcapstone")
    try:
        text = (Path(home) / "config" / "alert.env").read_text(encoding="utf-8")
    except OSError:
        return "inline"
    keys = {
        ln.split("=", 1)[0].strip().removeprefix("export ").strip()
        for ln in text.splitlines()
        if "=" in ln and not ln.lstrip().startswith("#") and ln.split("=", 1)[1].strip()
    }
    if "SKALERT_TALK_ROOM" in keys:
        return "talk"
    if "SKALERT_CHAT_ID" in keys or "TELEGRAM_BOT_TOKEN" in keys:
        return "telegram"
    return "inline"


def _short(text: str, n: int = 160) -> str:
    return " ".join(str(text).split())[:n]


def ensure_intention(
    item_id: str, purpose: str, title: str, due: datetime, *, channel: str | None = None
) -> tuple[str, bool]:
    """Create the intention ``gtd-<item>-<purpose>`` once. Returns (id, created)."""
    api = _require()
    iid = f"gtd-{item_id}-{purpose}"
    store = api.open_store()
    try:
        if store.get(iid) is not None:
            return iid, False
        store.save(
            api.Intention(
                id=iid,
                kind="time",
                title=_short(title),
                due_at=due,
                channel=channel or alert_channel(),
                origin_memory_id=item_id,
            )
        )
        return iid, True
    finally:
        store.close()


def overdue_intentions(now: datetime | None = None) -> list[dict]:
    """Overdue pending/armed time intentions, most overdue first. [] when unavailable."""
    api = _load_api()
    if api is None:
        return []
    now = now or datetime.now(timezone.utc)
    try:
        store = api.open_store()
    except Exception as exc:  # noqa: BLE001
        logger.warning("intentions store unreadable: %s", exc)
        return []
    out = []
    try:
        for state in _ACTIVE_STATES:
            for i in store.list(state=state):
                due = getattr(i, "due_at", None)
                snooze = getattr(i, "snooze_until", None)
                if getattr(i, "kind", None) != "time" or due is None or due > now:
                    continue
                if snooze is not None and snooze > now:
                    continue
                out.append(
                    {
                        "id": i.id,
                        "title": i.title,
                        "due_at": due,
                        "origin": getattr(i, "origin_memory_id", None),
                        "overdue_days": (now - due).days,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("intentions listing failed: %s", exc)
        return []
    finally:
        store.close()
    out.sort(key=lambda d: d["due_at"])
    return out


def sweep_waiting(
    items: list[dict], now: datetime | None = None, *, min_age_days: int = STALE_WAITING_DAYS
) -> list[str]:
    """One intention per ownerless waiting-for item older than ``min_age_days``.

    Idempotent, keyed on the GTD item id. Returns the ids created this run.
    """
    _require()
    now = now or datetime.now(timezone.utc)
    created: list[str] = []
    for it in items:
        item_id = str(it.get("id") or "")
        if not item_id or (it.get("delegate_to") or "").strip():
            continue
        try:
            ts = datetime.fromisoformat(str(it.get("created_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if (now - ts).days <= min_age_days:
            continue
        iid, new = ensure_intention(
            item_id, "stale", f"Waiting-for has no owner: {it.get('text', '')}", now
        )
        if new:
            created.append(iid)
    return created
