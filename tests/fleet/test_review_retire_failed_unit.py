"""An unused offer can outlive a failed transient unit from an older run."""

import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_review_custody as custody
from skcapstone.fleet import review_retire as retire
from tests.fleet.test_review_admission_retire import pending  # noqa: F401


@pytest.fixture
def unused(pending):  # noqa: F811
    p = pending
    p.status_path.unlink()
    p.events.pop()
    p.request["lease_expires_at"] = "2020-01-01T00:00:00Z"
    p.request_path.write_text(json.dumps(p.request))
    p.events[0].update(
        ts="2019-12-31T23:00:00Z", request_sha256=retire.production_builder.digest(p.request)
    )
    p.state["Transient"] = "yes"
    return p


def test_failed_unused_offer_retires_without_reset_and_receipt_is_recognized(unused):
    p = unused
    result = retire.recover_failed_generations(p.paths, p.home)
    assert result[0]["state"] == "retired-prestart", result
    assert retire.retired_offers(p.home, p.card, p.events) == {p.request["request_id"]}
    assert not p.request_path.exists()
    assert not any("reset-failed" in call or "stop" in call for call in p.calls)
    assert any("systemctl" in call and "chiap03" in call for call in p.calls)


@pytest.mark.parametrize(
    "change",
    [
        {"ActiveState": "active"},
        {"ActiveState": "activating"},
        {"ActiveState": "unknown"},
        {"ActiveState": "inactive", "SubState": "dead"},
        {"MainPID": "12"},
        {"ControlPID": "12"},
        {"SubState": "unknown"},
        {"LoadState": "error"},
        {"Transient": "no"},
        {"Transient": ""},
    ],
)
def test_unused_offer_refuses_unproven_unit(unused, change):
    unused.state.update(change)
    assert retire.recover_failed_generations(unused.paths, unused.home)[0]["state"] == "refused"
    assert unused.request_path.exists()


@pytest.mark.parametrize("evidence", ["status", "launch", "session", "unreachable"])
def test_unused_offer_requires_absent_execution_evidence(unused, monkeypatch, evidence):
    p = unused
    if evidence == "status":
        p.status["state"] = "running"
        p.status_path.write_text(json.dumps(p.status))
    elif evidence == "launch":
        p.events.append(
            dict(
                action="review_assignment_launch",
                recommendation_id=p.request["request_id"],
                launched=True,
            )
        )
    else:
        original = retire.subprocess.run

        def run(command, **kw):
            if evidence == "unreachable":
                return SimpleNamespace(returncode=255, stdout="")
            if "tmux" in command:
                return SimpleNamespace(returncode=0, stdout="pi-deepseek-" + p.card)
            return original(command, **kw)

        monkeypatch.setattr(retire.subprocess, "run", run)
    result = retire.recover_failed_generations(p.paths, p.home)
    assert not any(row["state"].startswith("retired") for row in result), result
    assert p.request_path.exists()


def test_launched_unit_still_requires_exact_invocation(unused):
    with pytest.raises(ValueError, match="exact managed worker death unproven"):
        custody.unit_terminal(unused.unit, "d" * 32, host="chiap03")
