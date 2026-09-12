from datetime import datetime, timedelta, timezone

from skcapstone.agent_projection import display_state, projection_summary
from skcapstone.coordination import AgentFile, AgentState

NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def agent(*, state=AgentState.ACTIVE, task=None, age_seconds=0):
    return AgentFile(
        agent="tester",
        state=state,
        current_task=task,
        last_seen=(NOW - timedelta(seconds=age_seconds)).isoformat(),
    )


def test_current_task_and_fresh_heartbeat_are_active():
    assert display_state(agent(task="8d6b4e2c"), now=NOW) == "active"


def test_fresh_projection_without_current_task_is_idle():
    assert display_state(agent(), now=NOW) == "idle"


def test_stored_active_with_old_heartbeat_is_labeled_stale():
    assert display_state(agent(task="old-card", age_seconds=901), now=NOW) == "stale"


def test_malformed_or_future_heartbeat_fails_closed_as_stale():
    malformed = agent(task="card")
    malformed.last_seen = "not-a-timestamp"
    assert display_state(malformed, now=NOW) == "stale"
    assert display_state(agent(task="card", age_seconds=-1), now=NOW) == "stale"


def test_explicit_offline_remains_offline():
    assert display_state(agent(state=AgentState.OFFLINE), now=NOW) == "offline"


def test_projection_summary_groups_bounded_age_buckets_and_non_exact_tasks(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()

    def write(name, payload):
        agents.joinpath(f"{name}.json").write_text(__import__("json").dumps(payload))

    write(
        "idle-worker-a1b2c3d4",
        {"agent": "idle-worker-a1b2c3d4", "last_seen": NOW.isoformat(), "current_task": None},
    )
    write(
        "stale-worker-b2c3d4e5",
        {
            "agent": "stale-worker-b2c3d4e5",
            "last_seen": (NOW - timedelta(days=40)).isoformat(),
            "current_task": None,
        },
    )
    write(
        "recent-stale-worker-d4e5f6a7",
        {
            "agent": "recent-stale-worker-d4e5f6a7",
            "last_seen": (NOW - timedelta(minutes=30)).isoformat(),
            "current_task": None,
        },
    )
    write(
        "task-worker-c3d4e5f6",
        {
            "agent": "task-worker-c3d4e5f6",
            "last_seen": (NOW - timedelta(hours=2)).isoformat(),
            "current_task": "c3d4e5f6",
            "_claim_revision": "old",
        },
    )
    agents.joinpath("broken.json").write_text("{bad", encoding="utf-8")
    agents.joinpath("ghost.sync-conflict-node.json").write_text("{}", encoding="utf-8")

    summary = projection_summary(
        agents,
        now=NOW,
        folded_claim=lambda _card: ("other-owner", "new"),
    )

    assert summary["idle"] == {"under_1h": 1}
    assert summary["stale"] == {"30d_plus": 1, "under_1h": 1}
    assert summary["task_bearing_non_exact"] == {"1h_to_24h": 1}
    assert summary["malformed"] == {"unknown": 2}
