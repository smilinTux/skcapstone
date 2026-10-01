"""Local production exit preserves source custody and the legacy review path."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_exit
from tests.test_skfleet_worker_exit_evidence import _wrapper


@pytest.fixture
def exit_case(monkeypatch, tmp_path):
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", "/operator/policy.json")
    monkeypatch.setattr(production_exit, "_authority", lambda: "control")
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
    assert production_exit.disposition(args, terminal_proven=True) is None
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


def test_production_reviewer_keeps_existing_independent_finalizer(exit_case, monkeypatch):
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
    wrapper.finalize_worker_exit(args, SimpleNamespace(pid=123))
    assert calls == ["review-finalizer", "native-projection"]


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
