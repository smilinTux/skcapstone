"""Tests for the startup pillar-health check (coord c6323cce)."""

from __future__ import annotations

from skcapstone.health import degraded_pillars, startup_health_check
from skcapstone.models import AgentManifest, PillarStatus


def _healthy_manifest() -> AgentManifest:
    """A manifest whose pillars are all ACTIVE (skills MISSING = not degraded)."""
    m = AgentManifest(name="test-agent")
    m.identity.status = PillarStatus.ACTIVE
    m.memory.status = PillarStatus.ACTIVE
    m.trust.status = PillarStatus.ACTIVE
    m.consciousness.status = PillarStatus.ACTIVE
    m.security.status = PillarStatus.ACTIVE
    m.sync.status = PillarStatus.ACTIVE
    m.skills.status = PillarStatus.MISSING  # never installed → not a degradation
    return m


class TestDegradedPillars:
    def test_all_healthy_returns_empty(self):
        assert degraded_pillars(_healthy_manifest()) == {}

    def test_degraded_and_error_are_reported(self):
        m = _healthy_manifest()
        m.memory.status = PillarStatus.DEGRADED
        m.security.status = PillarStatus.ERROR
        result = degraded_pillars(m)
        assert result == {
            "memory": PillarStatus.DEGRADED,
            "security": PillarStatus.ERROR,
        }

    def test_missing_is_not_degraded(self):
        m = _healthy_manifest()
        m.sync.status = PillarStatus.MISSING
        assert "sync" not in degraded_pillars(m)


class TestStartupHealthCheck:
    def test_degraded_pillar_triggers_notification(self):
        m = _healthy_manifest()
        m.trust.status = PillarStatus.DEGRADED

        calls: list[tuple[str, str, str]] = []

        def fake_notifier(title: str, body: str, urgency: str) -> bool:
            calls.append((title, body, urgency))
            return True

        degraded = startup_health_check(m, notifier=fake_notifier)

        assert degraded == {"trust": PillarStatus.DEGRADED}
        assert len(calls) == 1
        title, body, urgency = calls[0]
        assert urgency == "critical"
        assert "trust" in body
        assert m.name in title

    def test_all_healthy_does_not_notify(self):
        m = _healthy_manifest()

        calls: list[tuple[str, str, str]] = []

        def fake_notifier(title: str, body: str, urgency: str) -> bool:
            calls.append((title, body, urgency))
            return True

        degraded = startup_health_check(m, notifier=fake_notifier)

        assert degraded == {}
        assert calls == []

    def test_notification_failure_does_not_raise(self):
        m = _healthy_manifest()
        m.memory.status = PillarStatus.ERROR

        def boom(title: str, body: str, urgency: str) -> bool:
            raise RuntimeError("notify backend down")

        # Must swallow the error and still report the degraded pillar.
        degraded = startup_health_check(m, notifier=boom)
        assert degraded == {"memory": PillarStatus.ERROR}
