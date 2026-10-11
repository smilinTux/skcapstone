"""An interrupted acceptance finish resumes instead of failing its own replay."""

import json
import socket

import pytest

from skcapstone.fleet import production_acceptance as acceptance
from skcapstone.fleet.production_review_evidence import ReviewEvidenceError

CARD, CLAIM = "569f0f50", "0" * 32


@pytest.fixture
def pending_finish(tmp_path, monkeypatch):
    home = tmp_path
    (home / "evidence/production-review-exits").mkdir(parents=True)
    (home / "evidence/production-review-exits" / f"{CARD}-{CLAIM}.json").write_text("{}")
    directory = acceptance.review_directory(home, CARD, CLAIM)
    directory.mkdir(parents=True)
    (directory / "context.json").write_text(json.dumps({"review": {}, "source": {}}))
    (directory / "context.json").chmod(0o600)
    directory.chmod(0o700)
    host = socket.gethostname().split(".")[0].lower()
    policy = {"authority_host": host}
    finished = []

    def replay(*args):
        raise ReviewEvidenceError("sealed review launch receipt is no longer valid")

    def finish(home_, directory_, context, *, guard):
        finished.append(directory_)
        return {"accepted": True}

    monkeypatch.setattr(
        "skcapstone.fleet.production_review_custody.import_remote_exits", lambda *a: None
    )
    monkeypatch.setattr(acceptance, "_retired_exit", lambda *a, **k: None)
    monkeypatch.setattr("skcapstone.review_verdict.recorded_verdict", lambda *a: "PASS")
    monkeypatch.setattr(acceptance, "_sealed_context_policy", replay)
    monkeypatch.setattr(acceptance, "finish_pair", finish)
    return home, directory, policy, finished


def test_interrupted_finish_resumes_without_replaying_the_receipt(pending_finish):
    home, directory, policy, finished = pending_finish
    (directory / "finish-intent.json").write_text("{}")
    (directory / "finish-intent.json").chmod(0o600)
    results = acceptance.reconcile(home, policy, process_check=lambda card: {})
    assert results == [{"card": CARD, "state": "accepted", "receipt": {"accepted": True}}]
    assert finished == [directory]


def test_before_finishing_the_receipt_replay_still_refuses(pending_finish):
    home, _directory, policy, finished = pending_finish
    results = acceptance.reconcile(home, policy, process_check=lambda card: {})
    assert results[0]["state"] == "pending"
    assert results[0]["reason"] == "sealed review launch receipt is no longer valid"
    assert finished == []
