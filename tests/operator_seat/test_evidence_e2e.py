"""End to end: REAL adapter, brief, proposer and loop, no stubbed stage.

The first round of gate 5 hardening compared the pass clock with a timestamp that
the envelope had stamped from that same pass clock, so evidence was always age 0
and the check could never fire. These tests drive the whole chain
(adapter -> envelope -> brief -> proposer -> loop) so that class of mistake cannot
hide between stages again.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.fleet import store
from skcapstone.operator_seat import act_dispatch, loop, proposer, skchat_adapter, skos_adapter

from .test_loop import _enroll

HUMAN = store.Writer(role="operator", node="node-158", identity="chef")
_CHAT_REPLY = json.dumps(
    [
        {
            "app": "skchat",
            "condition": "BridgeAlive",
            "action": "restart-telegram-bridge",
            "object": "telegram-bridge",
            "change_class": "normal",
            "rationale": "bridge poll heartbeat is stale while the daemon is up",
        }
    ]
)


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, prop, classification):
        self.calls.append(prop)
        return {"performed": True}


@pytest.fixture
def wedged_skchat(tmp_path, monkeypatch):
    """The real skchat adapter, observing a real (wedged) heartbeat file."""
    paths, _ = _enroll(tmp_path, monkeypatch)
    store.set_frozen(paths, False, writer=HUMAN, reason="test provisioning")
    heartbeat = tmp_path / "telegram_poll.ts"
    heartbeat.write_text("stale")
    old = time.time() - 1200
    os.utime(heartbeat, (old, old))
    monkeypatch.setenv("SKCHAT_BRIDGE_HEARTBEAT", str(heartbeat))
    monkeypatch.setenv("SKCOMMS_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setattr(skchat_adapter, "_probe_daemon_health", lambda: (True, True, True))
    monkeypatch.setattr(loop, "ADAPTERS", {"skchat": skchat_adapter.observe})
    return paths


def _chain(paths, *, now_iso, **kwargs):
    explain = act_dispatch.merged_explain()
    apply_fn = _Recorder()
    result = loop.run_once(
        paths,
        now_iso=now_iso,
        propose=lambda brief, route: proposer.propose(
            brief, explain, chat=lambda _prompt: _CHAT_REPLY
        ),
        explain=explain,
        apply_fn=apply_fn,
        execute=True,
        emit=lambda _s: None,
        **kwargs,
    )
    return result, apply_fn


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_real_wedge_observed_now_actuates(wedged_skchat):
    res, apply_fn = _chain(wedged_skchat, now_iso=_now_iso())
    assert res["brief"]["firing"], "the real adapter must see the wedge"
    assert len(apply_fn.calls) == 1
    stamp = apply_fn.calls[0]["evidence"]
    assert stamp["source"] == "observation:skchat/BridgeAlive/telegram-bridge"
    assert res["outcomes"][0]["outcome"] == "performed, unverified"


def test_a_pass_clock_in_2019_cannot_launder_an_observation_with_no_source_time(
    wedged_skchat, monkeypatch
):
    """The reviewed hole: stamp from the pass clock, check against the pass clock.

    The adapter below is the real skchat probe minus the one thing the envelope
    must never invent: a source time. With a pass clock of 2019 the old code
    stamped 2019 and compared 2019 with 2019: age 0, action ran.
    """

    def unstamped(paths=None, now_iso=None):
        payload = skchat_adapter.skchat_observe()
        payload.pop("source_observed_at", None)
        return payload

    monkeypatch.setattr(loop, "ADAPTERS", {"skchat": unstamped})
    res, apply_fn = _chain(wedged_skchat, now_iso="2019-01-01T00:00:00Z")
    assert apply_fn.calls == []
    outcome = res["outcomes"][0]["outcome"]
    assert outcome.startswith("refused: ")
    assert "missing" in outcome


def test_an_observation_that_ages_while_the_model_thinks_is_refused_at_the_decision(
    wedged_skchat,
):
    """Age is measured at the decision point on the real clock, not at pass start."""
    probe_clock = datetime.now(timezone.utc)
    ticks = iter([probe_clock + timedelta(seconds=3000)] * 50)
    res, apply_fn = _chain(
        wedged_skchat, now_iso=_now_iso(), max_evidence_age_s=600, clock=lambda: next(ticks)
    )
    assert apply_fn.calls == []
    assert "stale" in res["outcomes"][0]["outcome"]


def test_the_decision_clock_defaults_to_the_real_clock_not_the_pass_clock(wedged_skchat):
    # A pass clock far in the FUTURE must not make fresh evidence look stale either.
    res, apply_fn = _chain(wedged_skchat, now_iso="2099-01-01T00:00:00Z")
    assert len(apply_fn.calls) == 1


def test_a_stale_source_is_refused_even_when_the_pass_clock_is_current(tmp_path, monkeypatch):
    """A digest published in 2020 is stale evidence however fresh the probe is.

    The real skos adapter reports GradingBacklog from the digest, so its source
    time is the digest's own window end, not the moment Atlas looked at it.
    """
    paths, _ = _enroll(tmp_path, monkeypatch)
    store.set_frozen(paths, False, writer=HUMAN, reason="test provisioning")
    monkeypatch.setattr(
        skos_adapter,
        "_default_probe",
        lambda: {
            "scheduler_alive": True,
            "gtd_draining": True,
            "digest_fresh": True,
            "grading_backlog": True,
        },
    )
    monkeypatch.setattr(
        skos_adapter, "_read_digest", lambda: {"window": {"to": "2020-01-01T00:00:00Z"}}
    )
    monkeypatch.setattr(loop, "ADAPTERS", {"skos": skos_adapter.observe})
    reply = json.dumps(
        [
            {
                "app": "skos",
                "condition": "GradingBacklog",
                "action": "drain",
                "object": "grading-loop",
                "change_class": "normal",
                "rationale": "the grading backlog is high",
            }
        ]
    )
    explain = {
        "actions": [{"name": "drain", "standard": True, "reversible": True, "blast_radius": "low"}]
    }
    apply_fn = _Recorder()
    res = loop.run_once(
        paths,
        now_iso=_now_iso(),
        propose=lambda b, r: proposer.propose(b, explain, chat=lambda _p: reply),
        explain=explain,
        apply_fn=apply_fn,
        execute=True,
        emit=lambda _s: None,
    )
    assert res["brief"]["firing"], "the backlog must be firing"
    assert apply_fn.calls == []
    assert "stale" in res["outcomes"][0]["outcome"]
