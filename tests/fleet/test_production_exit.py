"""Production exit retains source/review custody while preserving legacy exits."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_exit
from tests.fleet.test_source_bundle import source  # noqa: F401
from tests.test_skfleet_worker_exit_evidence import _wrapper


@pytest.fixture
def exit_case(monkeypatch, tmp_path):
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", "/operator/policy.json")
    monkeypatch.setattr(production_exit, "_authority", lambda: "control")
    monkeypatch.setattr(production_exit, "release_blocked", lambda *args: None)
    row = SimpleNamespace(labels=["source-only"], title="Source", meta={}, links={})
    monkeypatch.setattr(
        production_exit, "CardStore", lambda home: SimpleNamespace(fold=lambda card: row)
    )
    args = SimpleNamespace(
        card="24b00001",
        owner="pi-codex-control-24b00001",
        claim_revision="a" * 32,
        source_repository="https://example.invalid/source.git",
        source_base_revision="b" * 40,
    )
    return args, row


def test_local_retry_rechecks_death_and_is_bounded(exit_case, monkeypatch):
    args, _ = exit_case
    attempts, proofs, sleeps = [], [], []

    def publish(*unused):
        attempts.append(True)
        if len(attempts) < 2:
            raise ValueError("replica delayed")
        return {"manifest_sha256": "c" * 64}

    monkeypatch.setattr(production_exit, "publish_source", publish)
    monkeypatch.setattr(production_exit.time, "sleep", sleeps.append)
    result = production_exit.retry_disposition(args, lambda: proofs.append(True) or True)
    assert result["state"] == "awaiting-review" and len(attempts) == 2
    assert len(proofs) == 2 and sleeps == [5]
    assert production_exit.retry_disposition(args, lambda: False) == result


def test_local_retry_stops_on_unproven_death(exit_case, monkeypatch):
    args, _ = exit_case
    calls = []

    def publish(*unused):
        calls.append(True)
        raise ValueError("pending")

    monkeypatch.setattr(production_exit, "publish_source", publish)
    monkeypatch.setattr(production_exit.time, "sleep", lambda value: None)
    proof = iter([True, False])
    result = production_exit.retry_disposition(args, lambda: next(proof))
    assert not result["process_terminal"] and not result["claim_released"]
    assert len(calls) == 1


def test_local_retry_exhaustion_retains_claim_without_indefinite_wrapper(exit_case, monkeypatch):
    args, _ = exit_case
    calls, sleeps = [], []

    def publish(*unused):
        calls.append(True)
        raise ValueError("not yet available")

    monkeypatch.setattr(production_exit, "publish_source", publish)
    monkeypatch.setattr(production_exit.time, "sleep", sleeps.append)
    result = production_exit.retry_disposition(args, lambda: True)
    assert result["state"] == "awaiting-evidence" and not result["claim_released"]
    assert len(calls) == 3 and sleeps == [5, 10]
    assert production_exit.retry_disposition(args, lambda: True) == result
    assert len(calls) == 3


@pytest.mark.parametrize(
    "invalid", [None, "claim", "owner", "base", "evidence", "referent", "superseded"]
)
def test_blocked_exact_native_release_or_preserved_custody(source, invalid):  # noqa: F811
    from skcoord.coordination import AgentFile, Board

    Board(source["home"]).save_agent(
        AgentFile(agent=source["owner"], claimed_tasks=[source["card"]])
    )
    outcome = {**source["outcome"], "verdict": "BLOCKED blocked_on=human referent=approval:test"}
    if invalid == "referent":
        outcome["verdict"] = "BLOCKED"
    if invalid == "claim":
        outcome["expected_claim_revision"] = "f" * 32
    owner = "different-owner" if invalid == "owner" else source["owner"]
    source["store"].append_event(source["card"], "verdict", owner, **outcome)
    if invalid == "superseded":
        source["store"].append_event(
            source["card"], "link", owner, link_key="verdict", value="PASS"
        )
    if invalid == "evidence":
        source["shared"].write_text("changed")
    request = dict(source["request"])
    if invalid == "base":
        request["base_revision"] = "f" * 40
    result = production_exit.release_blocked(
        source["home"], request, source["owner"], source["claim"]
    )
    row = source["store"].fold(source["card"])
    if invalid:
        assert result is None and row.owner == source["owner"]
    else:
        assert result["state"] == "blocked" and result["claim_released"]
        assert result["reason"] == outcome["verdict"] and row.owner is None
        assert str(row.status) != "done" and source["workspace"].exists()


def test_no_trusted_git_or_claim_release_without_exact_terminal_proof(exit_case, monkeypatch):
    args, _ = exit_case
    monkeypatch.setattr(
        production_exit, "publish_source", lambda *args, **kwargs: pytest.fail("process remains")
    )
    result = production_exit.disposition(args, terminal_proven=False)
    assert result["state"] == "awaiting-evidence"
    assert result["claim_released"] is False
    assert result["reason"] == "exact-process-and-cgroup-death-unproven"


@pytest.mark.parametrize("valid", [True, False])
def test_stopped_source_publishes_once_and_preserves_native_claim(exit_case, monkeypatch, valid):
    args, _ = exit_case
    calls = []

    def publish(home, request, owner, claim, workspace):
        calls.append(request)
        assert owner == args.owner and claim == args.claim_revision
        assert workspace == Path.cwd().resolve()
        if not valid:
            raise ValueError("typed candidate missing")
        return {"manifest_sha256": "c" * 64}

    monkeypatch.setattr(production_exit, "publish_source", publish)
    result = production_exit.disposition(args, terminal_proven=True)
    assert result["state"] == ("awaiting-review" if valid else "awaiting-evidence")
    assert result["claim_released"] is False
    assert production_exit.disposition(args, terminal_proven=True) == result
    assert calls == [
        {
            "card_id": args.card,
            "repository": args.source_repository,
            "base_revision": args.source_base_revision,
        }
    ]


def test_reviewer_and_legacy_outcomes_never_publish_producer_source(exit_case, monkeypatch):
    args, row = exit_case
    monkeypatch.setattr(
        production_exit, "publish_source", lambda *args, **kwargs: pytest.fail("not source")
    )
    row.labels.append("review")
    result = production_exit.disposition(args, terminal_proven=True)
    assert result["state"] == "awaiting-review-acceptance"
    assert result["claim_released"] is False
    monkeypatch.delenv("SKFLEET_PRODUCTION_POLICY")
    row.labels.remove("review")
    assert production_exit.disposition(args, terminal_proven=True) is None


def test_production_wrapper_bypasses_generic_release_for_pending_source(exit_case, monkeypatch):
    args, _ = exit_case
    wrapper = _wrapper()
    child = SimpleNamespace(pid=123)
    monkeypatch.setattr(wrapper, "terminal_local_evidence", lambda child: True)
    monkeypatch.setattr(
        production_exit, "publish_source", lambda *args: {"manifest_sha256": "c" * 64}
    )
    monkeypatch.setattr(
        wrapper, "finalize_terminal_capacity", lambda *args: pytest.fail("must retain claim")
    )
    records = []
    monkeypatch.setattr(
        wrapper, "write_process_record", lambda args, **kwargs: records.append(kwargs)
    )
    wrapper.finalize_worker_exit(args, child)
    assert records == [{"pid": 123, "completion_state": "awaiting-review"}]


def test_direct_seat_receipt_records_exact_terminal_runtime(monkeypatch, tmp_path, exit_case):
    args, _ = exit_case
    wrapper = _wrapper()
    args.lane = "glm"
    args.production_child_exit_code = 0
    args.production_source_disposition = {
        "state": "awaiting-review",
        "process_terminal": True,
        "claim_released": False,
    }
    monkeypatch.setattr(wrapper.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(wrapper.socket, "gethostname", lambda: "worker.example")
    monkeypatch.setenv("INVOCATION_ID", "e" * 32)
    wrapper.write_process_record(args, pid=123, completion_state="awaiting-review")

    receipt = json.loads(
        (tmp_path / ".skcapstone/fleet/direct-seats" / f"{args.owner}.json").read_text()
    )
    assert receipt["schema"] == "skfleet.direct-seat/v1"
    assert receipt["host"] == "worker"
    assert receipt["lane"] == args.lane
    assert receipt["unit"] == f"skfleet-worker-{args.lane}-{args.card}.service"
    assert receipt["invocation"] == "e" * 32
    assert receipt["exit_code"] == 0


def test_production_reviewer_retains_claim_instead_of_legacy_release(exit_case, monkeypatch):
    args, row = exit_case
    row.labels.append("review")
    wrapper = _wrapper()
    monkeypatch.setattr(wrapper, "terminal_local_evidence", lambda child: True)
    calls = []
    monkeypatch.setattr(
        wrapper,
        "finalize_terminal_capacity",
        lambda *args: calls.append("review-finalizer") or True,
    )
    monkeypatch.setattr(
        wrapper, "idle_owner_projection", lambda *args: calls.append("native-projection")
    )
    monkeypatch.setattr(wrapper, "write_process_record", lambda *args, **kwargs: None)
    wrapper.finalize_worker_exit(args, SimpleNamespace(pid=123, returncode=0))
    assert calls == []
    assert args.production_source_disposition["state"] == "awaiting-review-acceptance"


def test_wrapper_exact_source_arguments_are_explicit(monkeypatch):
    wrapper = _wrapper()
    monkeypatch.setattr(
        wrapper.sys,
        "argv",
        [
            "wrapper",
            "--card",
            "24b00001",
            "--owner",
            "worker",
            "--claim-revision",
            "a" * 32,
            "--host",
            "control",
            "--lane",
            "codex",
            "--model",
            "exact-model",
            "--stdout",
            "/tmp/out",
            "--evidence-dir",
            "/tmp/evidence",
            "--source-repository",
            "https://example.invalid/repo.git",
            "--source-base-revision",
            "b" * 40,
            "--",
            "/usr/bin/true",
        ],
    )
    args = wrapper.parse_args()
    assert args.source_repository == "https://example.invalid/repo.git"
    assert args.source_base_revision == "b" * 40
    assert args.command == ["/usr/bin/true"]
