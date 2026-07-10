"""Tests for skcapstone.dreaming_job — the jobs.yaml entrypoint for dreaming."""
from skcapstone import dreaming_job


def test_get_consciousness_loop_defaults_to_none():
    dreaming_job.set_consciousness_loop(None)
    assert dreaming_job.get_consciousness_loop() is None


def test_set_and_get_consciousness_loop_round_trips():
    sentinel = object()
    dreaming_job.set_consciousness_loop(sentinel)
    try:
        assert dreaming_job.get_consciousness_loop() is sentinel
    finally:
        dreaming_job.set_consciousness_loop(None)  # reset shared module state


def test_run_dreaming_job_skips_when_disabled(monkeypatch):
    from skcapstone.dreaming import DreamingConfig

    monkeypatch.setattr(
        dreaming_job, "load_dreaming_config", lambda home: DreamingConfig(enabled=False)
    )
    built = []
    monkeypatch.setattr(dreaming_job, "DreamingEngine", lambda **kw: built.append(kw))
    dreaming_job.run_dreaming_job()
    assert built == []


def test_run_dreaming_job_passes_registered_consciousness_loop(monkeypatch):
    from skcapstone.dreaming import DreamingConfig, DreamResult

    monkeypatch.setattr(
        dreaming_job, "load_dreaming_config", lambda home: DreamingConfig(enabled=True)
    )
    dreaming_job.set_consciousness_loop("fake-loop")
    captured = {}

    class FakeEngine:
        def __init__(self, **kw):
            captured.update(kw)

        def dream(self):
            return DreamResult(memories_created=["mem-1"])

    monkeypatch.setattr(dreaming_job, "DreamingEngine", FakeEngine)
    try:
        dreaming_job.run_dreaming_job()
    finally:
        dreaming_job.set_consciousness_loop(None)
    assert captured["consciousness_loop"] == "fake-loop"


def test_run_dreaming_job_works_with_no_consciousness_loop(monkeypatch):
    """Critical constraint: must not require a non-None consciousness_loop."""
    from skcapstone.dreaming import DreamingConfig, DreamResult

    monkeypatch.setattr(
        dreaming_job, "load_dreaming_config", lambda home: DreamingConfig(enabled=True)
    )
    dreaming_job.set_consciousness_loop(None)
    captured = {}

    class FakeEngine:
        def __init__(self, **kw):
            captured.update(kw)

        def dream(self):
            return DreamResult(skipped_reason="agent not idle")

    monkeypatch.setattr(dreaming_job, "DreamingEngine", FakeEngine)
    dreaming_job.run_dreaming_job()
    assert captured["consciousness_loop"] is None
