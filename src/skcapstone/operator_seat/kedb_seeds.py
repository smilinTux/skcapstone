"""Seed the ITIL KEDB with the known errors the operator adapters reference.

Every operator app adapter declares ``kedb_refs`` on its actions (e.g. skchat's
``restart-telegram-bridge`` -> ``ke-telegram-wedge``). Those ids have to resolve
to real KEDB entries, otherwise an operator brief points a human at a runbook id
that does not exist. This module seeds one KEDBEntry per referenced id, carrying
a clear symptom (what fires the condition), the known error (root cause), and the
workaround that matches the adapter action's own runbook.

Seeding is create-or-skip: an existing entry with that id is left exactly as it
is, never duplicated or overwritten, so it is safe to run every registration.

The drift guard (tests/operator_seat/test_kedb_seeds.py) walks the registered app
adapters' explain() actions and asserts every declared ``kedb_ref`` has a seed
here, so this set can never silently fall behind the adapters.
"""

from __future__ import annotations

from pathlib import Path

from ..itil import ITILManager

#: The knowledge base: one entry per ke-* id the app adapters reference. Each
#: workaround mirrors the runbook of the adapter action that names the id.
OPERATOR_KEDB_SEEDS: list[dict] = [
    {
        "id": "ke-skchat-daemon-down",
        "title": "skchat receive daemon down",
        "symptoms": [
            "DaemonReady is False",
            "the skchat daemon health endpoint (http://localhost:9385/health) is "
            "unreachable or reports not-ok",
        ],
        "root_cause": "the skchat receive daemon process is not running or is unhealthy.",
        "workaround": (
            "restart the skchat receive daemon "
            "(systemctl --user restart skchat-daemon.service) and verify DaemonReady."
        ),
        "tags": ["operator", "skchat", "daemon"],
    },
    {
        "id": "ke-telegram-wedge",
        "title": "telegram bridge silent wedge",
        "symptoms": [
            "BridgeAlive is False",
            "the telegram bridge last-poll heartbeat is older than 600s while the "
            "daemon is up (the ConnectTimeout hang signature, no new polls)",
        ],
        "root_cause": (
            "the telegram bridge poll loop hung on a ConnectTimeout and stopped "
            "polling without exiting, so it looks alive but delivers nothing."
        ),
        "workaround": (
            "restart the wedged telegram bridge "
            "(systemctl --user restart skchat-telegram-<agent>.service) to clear "
            "the silent-wedge signature."
        ),
        "tags": ["operator", "skchat", "telegram", "bridge"],
    },
    {
        "id": "ke-outbox-flood",
        "title": "skcomms outbox flood",
        "symptoms": [
            "OutboxBounded is False",
            "the skcomms outbox file count exceeds the bound (>1000 queued files); "
            "the 1.5M-tombstone flood signature",
        ],
        "root_cause": (
            "messages pile up faster than they drain (e.g. broadcast heartbeats "
            "flooding the outbox), so the queue grows without bound."
        ),
        "workaround": (
            "drop the stranded outbox messages (purge-outbox). This is "
            "irreversible, so it escalates as a MAJOR change for human approval "
            "before it runs; stop the flood source first."
        ),
        "tags": ["operator", "skchat", "skcomms", "outbox"],
    },
    {
        "id": "ke-skcode-hostd-down",
        "title": "skcode-hostd down",
        "symptoms": [
            "HostdReady is False",
            "the skcode-hostd API (http://localhost:9394/api/v1/hosts/self) does "
            "not answer on this host",
        ],
        "root_cause": "the skcode-hostd session-plane host process is down or unresponsive.",
        "workaround": ("systemctl --user restart skcode-hostd.service and verify HostdReady."),
        "tags": ["operator", "skcode", "hostd"],
    },
    {
        "id": "ke-skcode-session-wedge",
        "title": "skcode session wedge / runaway",
        "symptoms": [
            "SessionsHealthy is False",
            "a running skcode session has had no event for longer than the stale "
            "threshold (900s), i.e. it is wedged or running away",
        ],
        "root_cause": (
            "a session's PTY/tmux backing is stuck or looping, producing no new "
            "events past the wedge threshold."
        ),
        "workaround": (
            "archive the wedged session (stop + persist, never a destructive "
            "kill). Escalate to kill-runaway-session only when archive cannot "
            "recover it; that kill is irreversible and escalates as MAJOR with "
            "options."
        ),
        "tags": ["operator", "skcode", "session"],
    },
    {
        "id": "ke-skdashboard-down",
        "title": "skcapstone dashboard down",
        "symptoms": [
            "DashboardReady is False",
            "the dashboard status endpoint (http://127.0.0.1:7778/api/status) is "
            "unreachable or reports an error, or the board endpoint "
            "(http://127.0.0.1:7778/api/board) fails to load the coordination board",
        ],
        "root_cause": (
            "the skcapstone dashboard web server is not running or is unhealthy, so "
            "the coordination board cannot be served."
        ),
        "workaround": (
            "restart the skcapstone dashboard web server "
            "(systemctl --user restart skcapstone-dashboard.service) and verify "
            "DashboardReady."
        ),
        "tags": ["operator", "skdashboard", "dashboard", "board"],
    },
]

#: The ids this module knows how to seed (for the drift guard and callers).
SEEDED_IDS: frozenset[str] = frozenset(s["id"] for s in OPERATOR_KEDB_SEEDS)


def seed_operator_kedb(home, *, itil: ITILManager | None = None) -> list[str]:
    """Persist a KEDBEntry for each operator-adapter known error, create-or-skip.

    Args:
        home: ITIL home root (``~/.skcapstone`` or equivalent); used to build an
            ``ITILManager`` when one is not injected. Storage is fully injectable
            so tests never touch the real filesystem.
        itil: An optional pre-built ``ITILManager`` (tests pass one on tmp_path).

    Returns:
        The ids newly created this run (sorted). Ids whose entry already exists
        are skipped and not returned, so a second run returns an empty list.
    """
    manager = itil if itil is not None else ITILManager(Path(home).expanduser())
    manager.ensure_dirs()
    created: list[str] = []
    for seed in OPERATOR_KEDB_SEEDS:
        entry_path = manager.kedb_dir / f"{seed['id']}.json"
        if entry_path.exists():
            continue
        manager.create_kedb_entry(
            title=seed["title"],
            symptoms=list(seed["symptoms"]),
            root_cause=seed["root_cause"],
            workaround=seed["workaround"],
            managed_by="atlas",
            tags=list(seed.get("tags", [])),
            entry_id=seed["id"],
        )
        created.append(seed["id"])
    return sorted(created)


__all__ = ["OPERATOR_KEDB_SEEDS", "SEEDED_IDS", "seed_operator_kedb"]
