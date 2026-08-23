"""Tests for skcapstone.triggers - the proactive trigger daemon.

All firing is captured through an injected action sink and time is driven by an
injected clock, so these tests are hermetic: nothing touches the desktop or
wall-clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.triggers import (
    TriggerEngine,
    TriggerRule,
    default_config_path,
    load_triggers,
    load_triggers_with_dropins,
    register_condition,
    register_with_scheduler,
    system_metrics_context,
    unregister_condition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecordingSink:
    """Captures every action fire so tests can assert on them."""

    def __init__(self, dispatched: bool = True) -> None:
        self.calls: list[tuple] = []
        self._dispatched = dispatched

    def __call__(self, title, body, urgency="normal", dedup_key=None) -> bool:
        self.calls.append((title, body, urgency, dedup_key))
        return self._dispatched

    @property
    def count(self) -> int:
        return len(self.calls)


def _threshold_rule(name: str = "disk-low", cooldown: float = 300.0) -> TriggerRule:
    return TriggerRule(
        name=name,
        metric="disk_free_pct",
        op="<",
        value=10,
        title="Disk low",
        body="free space below 10%",
        urgency="critical",
        cooldown_seconds=cooldown,
    )


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_declarative_condition_met(self):
        rule = _threshold_rule()
        assert rule.evaluate({"disk_free_pct": 6.0}) is True

    def test_declarative_condition_unmet(self):
        rule = _threshold_rule()
        assert rule.evaluate({"disk_free_pct": 42.0}) is False

    def test_missing_metric_is_failsafe_false(self):
        rule = _threshold_rule()
        assert rule.evaluate({}) is False

    def test_unknown_operator_is_false(self):
        rule = TriggerRule(name="bad", metric="x", op="~=", value=1)
        assert rule.evaluate({"x": 5}) is False

    def test_non_comparable_types_do_not_raise(self):
        rule = TriggerRule(name="mixed", metric="x", op="<", value=1)
        # comparing str < int raises TypeError internally; must be swallowed
        assert rule.evaluate({"x": "hello"}) is False

    def test_neither_metric_nor_condition_is_false(self):
        rule = TriggerRule(name="empty")
        assert rule.evaluate({"anything": 1}) is False

    def test_named_condition(self):
        register_condition("always_true", lambda ctx: True)
        try:
            rule = TriggerRule(name="named", condition="always_true")
            assert rule.evaluate({}) is True
        finally:
            unregister_condition("always_true")

    def test_unknown_named_condition_is_false(self):
        rule = TriggerRule(name="named", condition="does_not_exist")
        assert rule.evaluate({}) is False

    def test_raising_named_condition_is_false(self):
        def _boom(ctx):
            raise RuntimeError("nope")

        register_condition("boom", _boom)
        try:
            rule = TriggerRule(name="named", condition="boom")
            assert rule.evaluate({}) is False
        finally:
            unregister_condition("boom")


# ---------------------------------------------------------------------------
# Engine tick / firing
# ---------------------------------------------------------------------------


class TestTick:
    def test_met_condition_fires_once(self):
        sink = RecordingSink()
        clock = FakeClock()
        engine = TriggerEngine([_threshold_rule()], action_sink=sink, clock=clock)

        fired = engine.tick({"disk_free_pct": 5.0})

        assert fired == ["disk-low"]
        assert sink.count == 1
        title, body, urgency, dedup_key = sink.calls[0]
        assert title == "Disk low"
        assert urgency == "critical"
        assert dedup_key == "trigger:disk-low"

    def test_cooldown_suppresses_repeat_fires(self):
        sink = RecordingSink()
        clock = FakeClock()
        engine = TriggerEngine([_threshold_rule(cooldown=300.0)], action_sink=sink, clock=clock)
        ctx = {"disk_free_pct": 5.0}

        assert engine.tick(ctx) == ["disk-low"]  # first fire
        # Condition still true, but within cooldown on subsequent ticks.
        assert engine.tick(ctx) == []
        clock.advance(299)
        assert engine.tick(ctx) == []
        assert sink.count == 1  # still just the one

    def test_fires_again_after_cooldown_lapses(self):
        sink = RecordingSink()
        clock = FakeClock()
        engine = TriggerEngine([_threshold_rule(cooldown=300.0)], action_sink=sink, clock=clock)
        ctx = {"disk_free_pct": 5.0}

        assert engine.tick(ctx) == ["disk-low"]
        clock.advance(301)
        assert engine.tick(ctx) == ["disk-low"]
        assert sink.count == 2
        assert engine.rules[0].fire_count == 2

    def test_unmet_condition_does_not_fire(self):
        sink = RecordingSink()
        engine = TriggerEngine([_threshold_rule()], action_sink=sink, clock=FakeClock())

        assert engine.tick({"disk_free_pct": 80.0}) == []
        assert sink.count == 0

    def test_disabled_rule_is_noop(self):
        sink = RecordingSink()
        rule = _threshold_rule()
        rule.enabled = False
        engine = TriggerEngine([rule], action_sink=sink, clock=FakeClock())

        assert engine.tick({"disk_free_pct": 1.0}) == []
        assert sink.count == 0

    def test_empty_rules_is_noop(self):
        sink = RecordingSink()
        engine = TriggerEngine([], action_sink=sink, clock=FakeClock())
        assert engine.tick({"disk_free_pct": 1.0}) == []
        assert sink.count == 0

    def test_none_context_does_not_fire_declarative(self):
        sink = RecordingSink()
        engine = TriggerEngine([_threshold_rule()], action_sink=sink, clock=FakeClock())
        assert engine.tick(None) == []
        assert sink.count == 0

    def test_sink_reporting_undispatched_does_not_start_cooldown(self):
        # If the notification layer suppresses the popup (returns False), the
        # rule must not record a fire - so a real dispatch can happen next tick.
        sink = RecordingSink(dispatched=False)
        clock = FakeClock()
        engine = TriggerEngine([_threshold_rule()], action_sink=sink, clock=clock)
        ctx = {"disk_free_pct": 5.0}

        assert engine.tick(ctx) == []
        assert engine.rules[0].last_fired is None
        assert engine.rules[0].fire_count == 0

    def test_raising_sink_does_not_crash_tick(self):
        def _boom(*a, **k):
            raise RuntimeError("sink down")

        engine = TriggerEngine([_threshold_rule()], action_sink=_boom, clock=FakeClock())
        # Must not raise; rule simply does not count as fired.
        assert engine.tick({"disk_free_pct": 5.0}) == []

    def test_multiple_rules_fire_independently(self):
        sink = RecordingSink()
        clock = FakeClock()
        rules = [
            _threshold_rule(name="disk-low"),
            TriggerRule(name="mem-high", metric="mem_pct", op=">=", value=90, body="mem"),
        ]
        engine = TriggerEngine(rules, action_sink=sink, clock=clock)

        fired = engine.tick({"disk_free_pct": 5.0, "mem_pct": 95})
        assert fired == ["disk-low", "mem-high"]
        assert sink.count == 2


# ---------------------------------------------------------------------------
# Callback action (opt-in)
# ---------------------------------------------------------------------------

# Module-level flag toggled by the callback fixture below.
_CALLBACK_HITS: list[dict] = []


def _record_callback(context):
    _CALLBACK_HITS.append(context)
    return True


class TestCallbackAction:
    def test_callback_action_invoked(self):
        _CALLBACK_HITS.clear()
        rule = TriggerRule(
            name="cb",
            metric="x",
            op=">",
            value=0,
            action="callback",
            callback=f"{__name__}:_record_callback",
        )
        engine = TriggerEngine([rule], action_sink=RecordingSink(), clock=FakeClock())
        assert engine.tick({"x": 1}) == ["cb"]
        assert _CALLBACK_HITS == [{"x": 1}]

    def test_invalid_callback_ref_does_not_fire(self):
        rule = TriggerRule(
            name="cb", metric="x", op=">", value=0, action="callback", callback="no-colon"
        )
        engine = TriggerEngine([rule], action_sink=RecordingSink(), clock=FakeClock())
        assert engine.tick({"x": 1}) == []

    def test_unknown_action_does_not_fire(self):
        rule = TriggerRule(name="weird", metric="x", op=">", value=0, action="teleport")
        engine = TriggerEngine([rule], action_sink=RecordingSink(), clock=FakeClock())
        assert engine.tick({"x": 1}) == []


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadTriggers:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert load_triggers(tmp_path / "nope.yaml") == []

    def test_loads_rules(self, tmp_path: Path):
        cfg = tmp_path / "triggers.yaml"
        cfg.write_text(
            "triggers:\n"
            "  disk-low:\n"
            "    metric: disk_free_pct\n"
            "    op: '<'\n"
            "    value: 10\n"
            "    body: low disk\n"
            "    urgency: critical\n"
            "    cooldown: 1h\n",
            encoding="utf-8",
        )
        rules = load_triggers(cfg)
        assert len(rules) == 1
        r = rules[0]
        assert r.name == "disk-low"
        assert r.metric == "disk_free_pct"
        assert r.op == "<"
        assert r.value == 10
        assert r.urgency == "critical"
        assert r.cooldown_seconds == 3600.0  # "1h" parsed

    def test_enabled_default_true(self, tmp_path: Path):
        cfg = tmp_path / "triggers.yaml"
        cfg.write_text(
            "triggers:\n  t:\n    condition: foo\n",
            encoding="utf-8",
        )
        assert load_triggers(cfg)[0].enabled is True

    def test_unknown_key_warns(self, tmp_path: Path):
        cfg = tmp_path / "triggers.yaml"
        cfg.write_text(
            "triggers:\n  t:\n    metric: x\n    op: '>'\n    value: 1\n    typpo: 5\n",
            encoding="utf-8",
        )
        with pytest.warns(UserWarning):
            load_triggers(cfg)

    def test_dropins_override_base(self, tmp_path: Path):
        base = tmp_path / "triggers.yaml"
        base.write_text(
            "triggers:\n  t:\n    metric: x\n    op: '>'\n    value: 1\n    body: base\n",
            encoding="utf-8",
        )
        dropdir = tmp_path / "triggers.d"
        dropdir.mkdir()
        (dropdir / "override.yaml").write_text(
            "triggers:\n  t:\n    metric: x\n    op: '>'\n    value: 1\n    body: dropin\n",
            encoding="utf-8",
        )
        with pytest.warns(UserWarning):
            rules = load_triggers_with_dropins(base)
        assert len(rules) == 1
        assert rules[0].body == "dropin"

    def test_end_to_end_loaded_rule_fires(self, tmp_path: Path):
        cfg = tmp_path / "triggers.yaml"
        cfg.write_text(
            "triggers:\n"
            "  hot:\n"
            "    metric: temp_c\n"
            "    op: '>='\n"
            "    value: 80\n"
            "    body: overheating\n",
            encoding="utf-8",
        )
        sink = RecordingSink()
        engine = TriggerEngine(load_triggers(cfg), action_sink=sink, clock=FakeClock())
        assert engine.tick({"temp_c": 85}) == ["hot"]
        assert engine.tick({"temp_c": 70}) == []


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


class _FakeScheduler:
    """Minimal stand-in for TaskScheduler.register."""

    def __init__(self) -> None:
        self.registered: list[tuple] = []

    def register(self, name, interval_seconds, callback, delay_first_run: float = 0.0):
        self.registered.append((name, interval_seconds, callback))
        return callback


class TestSchedulerIntegration:
    def test_register_wires_a_task_that_ticks(self):
        sink = RecordingSink()
        engine = TriggerEngine([_threshold_rule()], action_sink=sink, clock=FakeClock())
        sched = _FakeScheduler()

        state = {"disk_free_pct": 5.0}
        register_with_scheduler(sched, engine, context_provider=lambda: state, interval_seconds=15)

        assert len(sched.registered) == 1
        name, interval, cb = sched.registered[0]
        assert name == "proactive-triggers"
        assert interval == 15
        # Driving the registered callback runs a tick and fires the rule.
        cb()
        assert sink.count == 1

    def test_context_provider_error_does_not_crash_task(self):
        engine = TriggerEngine([_threshold_rule()], action_sink=RecordingSink(), clock=FakeClock())
        sched = _FakeScheduler()

        def _bad_provider():
            raise RuntimeError("cannot read state")

        register_with_scheduler(sched, engine, context_provider=_bad_provider)
        _, _, cb = sched.registered[0]
        # Must not raise.
        cb()


# ---------------------------------------------------------------------------
# Built-in context provider
# ---------------------------------------------------------------------------


class TestSystemMetricsContext:
    def test_returns_dict_and_never_raises(self):
        ctx = system_metrics_context()
        assert isinstance(ctx, dict)

    def test_reports_disk_free_pct_in_range(self):
        ctx = system_metrics_context()
        # disk_free_pct is the one metric available on every platform via shutil.
        assert "disk_free_pct" in ctx
        assert 0.0 <= ctx["disk_free_pct"] <= 100.0

    def test_provider_feeds_a_real_rule(self):
        # A rule that always holds (disk free below 200%) fires against the
        # live provider snapshot, proving the wiring end-to-end.
        rule = TriggerRule(name="always", metric="disk_free_pct", op="<", value=200)
        engine = TriggerEngine([rule], action_sink=RecordingSink(), clock=FakeClock())
        assert engine.tick(system_metrics_context()) == ["always"]


class TestDefaultConfigPath:
    def test_ends_with_expected_suffix(self, tmp_path):
        p = default_config_path(home=tmp_path)
        assert p == tmp_path / "config" / "triggers.yaml"
