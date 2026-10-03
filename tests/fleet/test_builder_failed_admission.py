"""Normal builder reconciliation finalizes exact failed, unobserved launches."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_dispatch as builder
from skcapstone.fleet import production_admission as admission
from skcapstone.fleet import production_builder as production
from tests.fleet.test_builder_dispatch import _card, _folded
from tests.fleet.test_production_builder import production_setup as production_setup


@pytest.fixture
def pending(paths, production_setup, monkeypatch, tmp_path):
    request = builder.offer(paths, _card(), ["source-only", "sk-m"],
                            writer=production_setup.writer)
    monkeypatch.setattr(production.socket, "gethostname", lambda: "worker")
    card = _folded(archived=False)

    def claim(_board, owner, card_id):
        card.owner = owner
        card.meta["_claim_revision"] = "a" * 32

    monkeypatch.setattr(builder.Board, "claim_task", claim)
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: card)
    monkeypatch.setattr(builder, "startup_hello", lambda *args, **kwargs: None)
    monkeypatch.setattr(admission, "card_revision", lambda row: "b" * 64)
    monkeypatch.setattr(admission, "card_mutation_lock", lambda *args: nullcontext())
    launched = []

    def lost_ack(command, workspace):
        status = builder._load(builder.status_path(paths, "node-worker", request["card_id"]))
        assert status["admission_contract"]["argv"] == [command[0], *command[2:]]
        launched.append(command)
        raise OSError("fixture lost spawn acknowledgement")

    builder.consume_one(paths, tmp_path, "node-worker", launcher=lost_ack,
                        materializer=lambda request, workspace: workspace)
    assert len(launched) == 1
    status = builder._load(builder.status_path(paths, "node-worker", request["card_id"]))
    marker = launched[0][1].split("=", 2)[2]
    directory = tmp_path / "fleet/resource-admission/worker" / marker
    values = dict(Id=status["unit"], LoadState="loaded", ActiveState="failed",
                  SubState="failed", InvocationID="c" * 32, SKFLEET_ADMISSION_ID=marker,
                  MemoryMax=str(request["production"]["resources"]["memory_max_bytes"]),
                  MainPID="0", ControlPID="0", ExecMainPID="999999999",
                  ExecMainCode="1", ExecMainStatus="203", Result="exit-code",
                  ControlGroup="", TasksCurrent="[not set]")
    monkeypatch.setattr(admission, "unit_state", lambda *args, **kwargs: dict(values))
    return SimpleNamespace(home=tmp_path, request=request, status=status, values=values,
                           directory=directory, card=card)


def occupancy(pending):
    with admission._transaction(pending.home, "worker") as root:
        return admission._occupancy(root, pending.home)


def test_real_reconciliation_finalizes_but_preserves_claim_and_outcome(
    pending, paths, monkeypatch
):
    from skcapstone.fleet import builder_continue, builder_terminal, production_exit, source_bundle

    assert occupancy(pending)[0]["reserved_memory_max"] > 0
    finalizer = admission.finalize_failed_launch

    def checked_finalizer(*args, **kwargs):
        try:
            return finalizer(*args, **kwargs)
        except (ValueError, OSError) as error:
            raise AssertionError("positive fixture finalization failed") from error

    monkeypatch.setattr(admission, "finalize_failed_launch", checked_finalizer)
    monkeypatch.setattr(production, "service_state", lambda status, process:
                        (False, 203) if status.get("invocation") == "c" * 32 else (None, None))
    monkeypatch.setattr(builder_terminal, "observe", lambda *args: None)
    monkeypatch.setattr(builder_continue, "original_outcome_pending", lambda *args: False)
    monkeypatch.setattr(production_exit, "release_blocked", lambda *args: None)

    def no_outcome(*args, **kwargs):
        raise source_bundle.SourceBundleError("fixture has no candidate")

    monkeypatch.setattr(source_bundle, "publish_source", no_outcome)
    result = builder._reconcile_running(paths, pending.home, "node-worker",
                                        pending.request, pending.status)
    assert result["state"] == "awaiting-evidence" and result["claim_released"] is False
    assert result["admission_terminal"]["process_absent"] is True
    assert result["admission_contract"] == pending.status["admission_contract"]
    assert (pending.directory / "failed-terminal.json").is_file()
    assert not (pending.directory / "observed.json").exists()
    assert occupancy(pending) == []
    receipt = (pending.directory / "failed-terminal.json").read_bytes()
    # A daemon restart reads the persisted contract; a lost receipt reply is safe.
    replay = dict(pending.status)
    replay.pop("admission_terminal")
    builder._finalize_builder_admission(pending.home, pending.request, replay)
    assert (pending.directory / "failed-terminal.json").read_bytes() == receipt
    assert pending.card.owner == result["owner"]


@pytest.mark.parametrize("change", ["absent", "live", "invocation", "claim", "revision",
                                     "argv", "request", "production", "observed"])
def test_unproved_failure_stays_charged(pending, monkeypatch, change):
    if change == "absent":
        pending.values.update(LoadState="not-found", ActiveState="inactive", InvocationID="")
    elif change == "live":
        pending.values["ActiveState"] = "active"
    elif change == "invocation":
        pending.status["invocation"] = "d" * 32
    elif change == "claim":
        pending.card.owner = "another-owner"
    elif change == "revision":
        monkeypatch.setattr(admission, "card_revision", lambda row: "d" * 64)
    elif change == "argv":
        pending.status["admission_contract"]["argv"][-1] = "changed-source-command"
    elif change == "request":
        pending.request["request_id"] = "d" * 64
    elif change == "production":
        pending.request = dict(pending.request, production={})
    else:
        (pending.directory / "observed.json").write_text(json.dumps({}))
    builder._finalize_builder_admission(pending.home, pending.request, pending.status)
    assert not (pending.directory / "failed-terminal.json").exists()
    assert "admission_terminal" not in pending.status
    assert (pending.directory / "intent.json").is_file()
