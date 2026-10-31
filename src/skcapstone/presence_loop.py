"""PRES-6 proactive presence loop: notice unanswered Talk mentions and DMs.

The daemon already owns one scheduler thread
(:class:`skcapstone.scheduled_tasks.TaskScheduler`).  This module registers one
more task on it, ``presence_loop``, that wakes every ``interval_seconds``
(default 15 minutes), reads the agent's Nextcloud Talk room list, and flags
interactions that are waiting on the agent:

* **one-to-one rooms** (Talk room type 1) whose last message is from someone
  else and is older than ``threshold_minutes``;
* **group rooms** where the agent has an unread mention (``unreadMention``)
  and the last message is from someone else and older than the threshold.

System messages, bot actors, the agent's own messages, read-only rooms and
note-to-self / changelog rooms are never flagged.

Safety posture:

* **Off by default.**  Nothing is registered unless
  ``SKCAPSTONE_PRESENCE_ENABLED`` is truthy.
* **Dry-run by default.**  ``SKCAPSTONE_PRESENCE_MODE`` defaults to
  ``dry-run``: detection runs and is logged, no outreach is sent and no state
  is written.  ``live`` must be set explicitly.
* **Read-only detection.**  The only call made in dry-run is a ``GET`` of the
  room list.  It never marks anything read.
* **Once per message.**  Outreach is keyed on ``(room token, message id)`` and
  persisted, so a still-unanswered message triggers once, not every tick.
* **Guardrails.**  A per-tick cap (``max_per_tick``) and local quiet hours
  (default 22:00 to 07:00) defer outreach; deferred items are not recorded, so
  they fire on the first tick after quiet hours end.

Outreach goes through an injectable sink.  The default sink is the existing
:func:`skcapstone.notifications.notify` path (desktop or headless sk-alert),
which tells the operator something is waiting.  Posting a nudge into the Talk
room itself is opt-in with ``SKCAPSTONE_PRESENCE_TALK_NUDGE=1``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("skcapstone.presence_loop")

ROOM_TYPE_ONE_TO_ONE = 1
ROOM_TYPE_GROUP = 2
ROOM_TYPE_PUBLIC = 3
_WATCHED_TYPES = frozenset({ROOM_TYPE_ONE_TO_ONE, ROOM_TYPE_GROUP, ROOM_TYPE_PUBLIC})
_IGNORED_ACTOR_TYPES = frozenset({"bots", "bridged"})

DEFAULT_INTERVAL_SECONDS = 900
DEFAULT_THRESHOLD_MINUTES = 15
DEFAULT_MAX_PER_TICK = 3
DEFAULT_QUIET_START = 22
DEFAULT_QUIET_END = 7

NUDGE_TEMPLATE = "Still here. I saw this and haven't answered yet; it's next on my list."

# (method, path, body) -> parsed JSON.  Injectable so tests never touch the net.
Fetcher = Callable[[str, str, Optional[dict]], Any]
# (title, body, urgency, dedup_key=...) -> dispatched?  Mirrors notifications.notify.
OutreachSink = Callable[..., bool]


def _truthy(value: Optional[str]) -> bool:
    """Return True for the usual truthy env spellings."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class PresenceConfig:
    """Settings for the presence loop, normally read from the environment.

    Attributes:
        enabled: Register the task at all.
        mode: ``dry-run`` (detect and log only) or ``live`` (send outreach).
        interval_seconds: Scheduler cadence.
        threshold_minutes: How long a message must wait before it counts.
        max_per_tick: Outreach cap per tick.
        quiet_start: Local hour quiet hours begin (inclusive).
        quiet_end: Local hour quiet hours end (exclusive).
        talk_nudge: Also post a templated nudge into the Talk room (live only).
        server_url: Nextcloud base URL.
        username: The agent's Nextcloud user id.
        app_password: The agent's Nextcloud app password.
    """

    enabled: bool = False
    mode: str = "dry-run"
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES
    max_per_tick: int = DEFAULT_MAX_PER_TICK
    quiet_start: int = DEFAULT_QUIET_START
    quiet_end: int = DEFAULT_QUIET_END
    talk_nudge: bool = False
    server_url: str = ""
    username: str = ""
    app_password: str = field(default="", repr=False)

    @property
    def live(self) -> bool:
        """True only when ``mode`` is exactly ``live``."""
        return self.mode == "live"

    @property
    def has_credentials(self) -> bool:
        """True when all three Talk credentials are present."""
        return bool(self.server_url and self.username and self.app_password)

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "PresenceConfig":
        """Build a config from environment variables.

        Args:
            env: Mapping to read (defaults to ``os.environ``).

        Returns:
            The parsed config.  Bad integers fall back to their defaults.
        """
        e = os.environ if env is None else env

        def _int(key: str, default: int) -> int:
            try:
                return int(e.get(key, default))
            except (TypeError, ValueError):
                logger.warning("presence: bad %s=%r, using %d", key, e.get(key), default)
                return default

        mode = (e.get("SKCAPSTONE_PRESENCE_MODE") or "dry-run").strip().lower()
        if mode not in {"dry-run", "live"}:
            logger.warning("presence: unknown mode %r, using dry-run", mode)
            mode = "dry-run"
        return cls(
            enabled=_truthy(e.get("SKCAPSTONE_PRESENCE_ENABLED")),
            mode=mode,
            interval_seconds=max(
                60, _int("SKCAPSTONE_PRESENCE_INTERVAL", DEFAULT_INTERVAL_SECONDS)
            ),
            threshold_minutes=max(
                0, _int("SKCAPSTONE_PRESENCE_THRESHOLD_MIN", DEFAULT_THRESHOLD_MINUTES)
            ),
            max_per_tick=max(0, _int("SKCAPSTONE_PRESENCE_MAX_PER_TICK", DEFAULT_MAX_PER_TICK)),
            quiet_start=_int("SKCAPSTONE_PRESENCE_QUIET_START", DEFAULT_QUIET_START) % 24,
            quiet_end=_int("SKCAPSTONE_PRESENCE_QUIET_END", DEFAULT_QUIET_END) % 24,
            talk_nudge=_truthy(e.get("SKCAPSTONE_PRESENCE_TALK_NUDGE")),
            server_url=(e.get("NEXTCLOUD_TALK_SERVER_URL") or "").rstrip("/"),
            username=e.get("NEXTCLOUD_TALK_USERNAME") or "",
            app_password=e.get("NEXTCLOUD_TALK_APP_PASSWORD") or "",
        )


@dataclass(frozen=True)
class Pending:
    """One interaction waiting on the agent.

    Attributes:
        token: Talk room token.
        room_name: Room display name.
        kind: ``dm`` or ``mention``.
        message_id: Id of the last (unanswered) message.
        actor: Display name of who wrote it.
        waited_minutes: How long it has been waiting.
        preview: First 120 characters of the message.
    """

    token: str
    room_name: str
    kind: str
    message_id: int
    actor: str
    waited_minutes: int
    preview: str

    @property
    def key(self) -> str:
        """Stable dedup key for this message."""
        return f"{self.token}:{self.message_id}"


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """Return True when ``hour`` falls in the quiet window.

    Args:
        hour: Local hour 0-23.
        start: Window start hour (inclusive).
        end: Window end hour (exclusive).  ``start > end`` wraps midnight.
            ``start == end`` means no quiet hours.
    """
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def detect_pending(
    rooms: list[dict], self_id: str, now: float, threshold_minutes: int
) -> list[Pending]:
    """Pick the rooms whose last message is waiting on the agent.

    Args:
        rooms: Talk v4 room objects (``ocs.data`` of ``GET /room``).
        self_id: The agent's Nextcloud user id; its own messages never count.
        now: Current unix time.
        threshold_minutes: Minimum wait before a message counts.

    Returns:
        Pending items, longest-waiting first.
    """
    out: list[Pending] = []
    for room in rooms or []:
        rtype = room.get("type")
        if rtype not in _WATCHED_TYPES or room.get("readOnly"):
            continue
        last = room.get("lastMessage") or {}
        if not isinstance(last, dict) or not last.get("id"):
            continue
        if last.get("systemMessage"):
            continue
        if last.get("actorType") in _IGNORED_ACTOR_TYPES:
            continue
        if last.get("actorId") == self_id:
            continue
        if rtype == ROOM_TYPE_ONE_TO_ONE:
            kind = "dm"
        elif room.get("unreadMention"):
            kind = "mention"
        else:
            continue
        waited = (now - float(last.get("timestamp") or now)) / 60.0
        if waited < threshold_minutes:
            continue
        text = " ".join(str(last.get("message") or "").split())
        out.append(
            Pending(
                token=str(room.get("token") or ""),
                room_name=str(room.get("displayName") or room.get("name") or "?"),
                kind=kind,
                message_id=int(last["id"]),
                actor=str(last.get("actorDisplayName") or last.get("actorId") or "?"),
                waited_minutes=int(waited),
                preview=text[:120],
            )
        )
    out.sort(key=lambda p: p.waited_minutes, reverse=True)
    return out


class PresenceState:
    """Persisted set of message keys that already got outreach.

    Args:
        path: JSON file to keep state in.
        keep: Max keys kept (oldest dropped first).
    """

    def __init__(self, path: Path, keep: int = 500) -> None:
        self._path = path
        self._keep = keep
        self._seen: dict[str, float] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._seen = {str(k): float(v) for k, v in (data.get("seen") or {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as exc:
            logger.warning("presence: unreadable state %s (%s); starting empty", path, exc)

    def seen(self, key: str) -> bool:
        """True when ``key`` already got outreach."""
        return key in self._seen

    def mark(self, key: str, when: float) -> None:
        """Record outreach for ``key`` and persist atomically."""
        self._seen[key] = when
        if len(self._seen) > self._keep:
            for old in sorted(self._seen, key=lambda k: self._seen[k])[
                : len(self._seen) - self._keep
            ]:
                del self._seen[old]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": self._seen}, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)


def make_talk_fetcher(cfg: PresenceConfig, timeout: float = 20.0) -> Fetcher:
    """Return a fetcher that calls the Talk OCS API with the agent's app password.

    Args:
        cfg: Config carrying server URL and credentials.
        timeout: Per-request timeout in seconds.
    """
    token = base64.b64encode(f"{cfg.username}:{cfg.app_password}".encode()).decode()

    def _fetch(method: str, path: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(cfg.server_url + path, data=data, method=method)
        req.add_header("Authorization", f"Basic {token}")
        req.add_header("OCS-APIRequest", "true")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https base
            return json.loads(resp.read().decode("utf-8"))

    return _fetch


ROOMS_PATH = "/ocs/v2.php/apps/spreed/api/v4/room?format=json"


class PresenceLoop:
    """One presence check per :meth:`tick`.

    Args:
        cfg: Loop configuration.
        fetcher: Talk API caller.
        state: Outreach dedup state.
        sink: Operator outreach sink (defaults to ``notifications.notify``).
        clock: Wall clock (unix seconds).
        hour_fn: Returns the current local hour (for quiet hours).
    """

    def __init__(
        self,
        cfg: PresenceConfig,
        fetcher: Fetcher,
        state: PresenceState,
        sink: Optional[OutreachSink] = None,
        clock: Callable[[], float] = time.time,
        hour_fn: Callable[[], int] = lambda: datetime.now().hour,
    ) -> None:
        self.cfg = cfg
        self._fetch = fetcher
        self._state = state
        self._clock = clock
        self._hour = hour_fn
        if sink is None:
            from .notifications import notify as sink
        self._sink = sink

    def tick(self) -> dict:
        """Run one check.  Never raises.

        Returns:
            A summary: ``pending`` (all detected), ``sent``, ``deferred`` and
            ``skipped_seen`` key lists, plus ``mode`` and ``error``.
        """
        report: dict = {
            "mode": self.cfg.mode,
            "pending": [],
            "sent": [],
            "deferred": [],
            "skipped_seen": [],
            "error": None,
        }
        try:
            payload = self._fetch("GET", ROOMS_PATH, None)
            rooms = (payload.get("ocs") or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001 - must not crash the scheduler
            report["error"] = f"room list failed: {exc}"
            logger.warning("presence: %s", report["error"])
            return report

        now = self._clock()
        pending = detect_pending(rooms, self.cfg.username, now, self.cfg.threshold_minutes)
        report["pending"] = [p.key for p in pending]
        quiet = in_quiet_hours(self._hour(), self.cfg.quiet_start, self.cfg.quiet_end)

        for item in pending:
            if self._state.seen(item.key):
                report["skipped_seen"].append(item.key)
                continue
            if not self.cfg.live:
                logger.info(
                    "presence[dry-run]: would reach out: %s %s from %s, %d min: %s",
                    item.kind,
                    item.room_name,
                    item.actor,
                    item.waited_minutes,
                    item.preview,
                )
                continue
            if quiet or len(report["sent"]) >= self.cfg.max_per_tick:
                report["deferred"].append(item.key)
                continue
            if self._outreach(item):
                self._state.mark(item.key, now)
                report["sent"].append(item.key)

        if pending:
            logger.info(
                "presence tick (%s): %d pending, %d sent, %d deferred",
                self.cfg.mode,
                len(pending),
                len(report["sent"]),
                len(report["deferred"]),
            )
        return report

    def _outreach(self, item: Pending) -> bool:
        """Send outreach for one item; True when at least one channel accepted it."""
        title = f"Unanswered {'DM' if item.kind == 'dm' else 'mention'}: {item.room_name}"
        body = f"{item.actor}, waiting {item.waited_minutes} min: {item.preview}"
        ok = False
        try:
            ok = bool(self._sink(title, body, "normal", dedup_key=f"presence:{item.key}"))
        except Exception as exc:  # noqa: BLE001
            logger.error("presence: sink failed for %s: %s", item.key, exc)
        if self.cfg.talk_nudge:
            try:
                self._fetch(
                    "POST",
                    f"/ocs/v2.php/apps/spreed/api/v1/chat/{item.token}?format=json",
                    {"message": NUDGE_TEMPLATE, "replyTo": item.message_id},
                )
                ok = True
            except Exception as exc:  # noqa: BLE001
                logger.error("presence: Talk nudge failed for %s: %s", item.key, exc)
        return ok


def make_presence_task(
    home: Path, env: Optional[dict] = None
) -> Optional[tuple[int, Callable[[], Any]]]:
    """Build the scheduler callback, or None when the loop is off or unusable.

    Args:
        home: Agent home; state lives at ``<home>/presence/state.json``.
        env: Environment mapping (defaults to ``os.environ``).

    Returns:
        ``(interval_seconds, callback)`` or ``None``.
    """
    cfg = PresenceConfig.from_env(env)
    if not cfg.enabled:
        return None
    if not cfg.has_credentials:
        logger.warning(
            "presence: enabled but NEXTCLOUD_TALK_* credentials missing; not registered"
        )
        return None
    loop = PresenceLoop(
        cfg, make_talk_fetcher(cfg), PresenceState(Path(home) / "presence" / "state.json")
    )
    return cfg.interval_seconds, loop.tick


def main() -> None:
    """``python -m skcapstone.presence_loop``: one dry-run tick, report as JSON."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = PresenceConfig.from_env()
    cfg.mode = "dry-run"
    if not cfg.has_credentials:
        raise SystemExit("NEXTCLOUD_TALK_SERVER_URL/USERNAME/APP_PASSWORD required")
    state = PresenceState(Path(os.devnull + ".unused"))
    report = PresenceLoop(cfg, make_talk_fetcher(cfg), state, sink=lambda *a, **k: False).tick()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
