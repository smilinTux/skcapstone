"""A mid-pass freeze is reported as what happened, not as "the whole pass stood down".

Review finding 3 and 6 on #1357: a freeze landing after an action had already been
verified was reported as the pass being frozen and "refused at the decision point",
which hid the action taken and the conditions still firing; and a real actuator
failure that coincided with a freeze was not counted against the retry budget.
"""

from __future__ import annotations

from skcapstone.fleet import store
from skcapstone.operator_seat import brief_publish, loop

from .test_loop import _enroll

HUMAN = store.Writer(role="operator", node="node-158", identity="chef")
NOW = "2026-08-20T00:00:00Z"
EXPLAIN = {"actions": [{"name": "restart", "standard": True, "reversible": True}]}


def _stamp():
    return {"source": "obs:demo", "observed_at": "2026-08-19T23:59:50Z"}


def _two_firing_objects(tmp_path, monkeypatch):
    """Two firing demo objects; the first action heals ITS object, then the kill switch lands."""
    from datetime import datetime, timezone

    paths, _ = _enroll(tmp_path, monkeypatch)
    store.set_frozen(paths, False, writer=HUMAN, reason="test provisioning")
    store.write_spec(
        paths,
        "operatorapp",
        "demo",
        {
            "name": "demo",
            "conditions": ["Ready"],
            "proposedStandardActions": ["restart"],
            "ratifiedStandardActions": ["restart"],
        },
        writer=HUMAN,
    )
    healed: set[str] = set()

    def observe(_paths, _now):
        return {
            "conditions": [
                {"type": "Ready", "status": "True" if obj in healed else "False", "object": obj}
                for obj in ("svc1", "svc2")
            ]
        }

    def apply_fn(prop, classification):
        healed.add(prop["object"])
        if prop["object"] == "svc1":
            store.set_frozen(paths, True, writer=HUMAN, reason="kill switch after svc1")
        return {"performed": True}

    monkeypatch.setattr(loop, "ADAPTERS", {"demo": observe})
    proposals = [
        {
            "app": "demo",
            "condition": "Ready",
            "action": "restart",
            "object": o,
            "evidence": _stamp(),
        }
        for o in ("svc1", "svc2")
    ]
    result = loop.run_once(
        paths,
        now_iso=NOW,
        propose=lambda b, r: proposals,
        explain=EXPLAIN,
        apply_fn=apply_fn,
        execute=True,
        require_verified_actions=True,
        clock=lambda: datetime(2026, 8, 20, 0, 0, 10, tzinfo=timezone.utc),
        emit=lambda _s: None,
    )
    return result


def test_a_freeze_after_a_verified_action_reports_what_was_done_and_what_is_left(
    tmp_path, monkeypatch
):
    res = _two_firing_objects(tmp_path, monkeypatch)
    first, second = res["outcomes"]
    assert first["outcome"] == "verified"
    assert first["freeze_state"] == "active"  # the state AT its decision point
    assert first["freeze_after"] == "frozen"  # a freeze after a SUCCESSFUL action is flagged too
    assert second["outcome"].startswith("refused: ")
    assert second["freeze_state"] == "frozen"
    assert res["frozen"] is True and res["frozen_mid_pass"] is True
    assert res["freeze_state"] == "frozen"
    summary = res["freeze_summary"]
    assert summary["taken_before_freeze"] == ["restart"]
    assert summary["refused_by_freeze"] == ["restart"]
    assert [(c["app"], c["type"], c["object"]) for c in summary["still_firing"]] == [
        ("demo", "Ready", "svc2")
    ]


def test_the_report_says_what_happened_not_that_the_whole_pass_was_refused(tmp_path, monkeypatch):
    res = _two_firing_objects(tmp_path, monkeypatch)
    report = res["report"]
    assert "1 action(s) taken before the freeze" in report
    assert "1 refused" in report
    assert "1 condition(s) still firing" in report
    assert "standing down" not in report


def test_the_published_brief_does_not_claim_atlas_stood_down(tmp_path, monkeypatch):
    res = _two_firing_objects(tmp_path, monkeypatch)
    md = brief_publish.render_markdown(res, NOW)
    html = brief_publish.render_html(res, NOW)
    assert "standing down" not in md and "standing down" not in html
    # The token the skos watchdog keys on is kept: the fleet IS frozen now.
    assert "**FROZEN**" in md
    assert "landed mid-pass" in md
    assert "1 action(s) taken before the freeze" in md
    assert "state: frozen=true mid_pass=true firing=1 stale=0 acted_before_freeze=1" in md
    assert "landed mid-pass" in html


def test_a_pass_that_starts_frozen_still_says_standing_down(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    store.set_frozen(paths, True, writer=HUMAN, reason="standing")
    res = loop.run_once(paths, now_iso=NOW, emit=lambda _s: None)
    md = brief_publish.render_markdown(res, NOW)
    assert "**FROZEN** - Atlas is standing down." in md
    assert res["freeze_summary"] is None


def test_no_freeze_means_no_freeze_summary(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    store.set_frozen(paths, False, writer=HUMAN, reason="test provisioning")
    res = loop.run_once(paths, now_iso=NOW, emit=lambda _s: None)
    assert res["freeze_summary"] is None
    assert "state: frozen=false" in brief_publish.render_markdown(res, NOW)


def test_a_real_failure_that_coincides_with_a_freeze_still_counts_against_the_budget(
    tmp_path, monkeypatch
):
    from datetime import datetime, timezone

    paths, _ = _enroll(tmp_path, monkeypatch)
    store.set_frozen(paths, False, writer=HUMAN, reason="test provisioning")
    state = loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=0)

    def freeze_then_fail_for_real(prop, classification):
        store.set_frozen(paths, True, writer=HUMAN, reason="coincides with a real fault")
        raise RuntimeError("disk exploded")

    res = loop.run_once(
        paths,
        now_iso=NOW,
        propose=lambda b, r: [
            {"action": "restart_service", "object": "web", "evidence": _stamp()}
        ],
        apply_fn=freeze_then_fail_for_real,
        execute=True,
        execution_state=state,
        clock=lambda: datetime(2026, 8, 20, 0, 0, 10, tzinfo=timezone.utc),
        emit=lambda _s: None,
    )
    assert res["frozen_mid_pass"] is True
    actions = state._load()["actions"]
    assert [e["consecutive_failures"] for e in actions.values()] == [1]
