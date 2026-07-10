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
