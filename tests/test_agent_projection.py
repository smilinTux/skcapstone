from datetime import datetime, timedelta, timezone

from skcapstone.agent_projection import display_state
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
