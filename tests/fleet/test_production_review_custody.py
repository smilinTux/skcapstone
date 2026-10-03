"""Production reviewer exit keeps the native claim until guarded acceptance."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_review_custody as custody


@pytest.fixture
def worker(tmp_path, monkeypatch):
    args = SimpleNamespace(
        card="ab000002",
        claim_revision="a" * 32,
        owner="pi-seraph-control-ab000002",
        host="control",
        lane="deepseek",
        model="current-model",
        source_base_revision="b" * 40,
        production_child_exit_code=0,
    )
    card = SimpleNamespace(owner=args.owner, meta={"_claim_revision": args.claim_revision})
    monkeypatch.setenv("INVOCATION_ID", "c" * 32)
    monkeypatch.setattr(custody.socket, "gethostname", lambda: "control")
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *a, **k: (
            "0::/user.slice/skfleet-worker-deepseek-ab000002.service\n"
            if str(self) == "/proc/self/cgroup"
            else original(self, *a, **k)
        ),
    )
    return tmp_path, args, card


def test_terminal_receipt_is_private_immutable_claim_bound_and_never_releases(worker):
    home, args, card = worker
    result = custody.retain_review_exit(home, args, card, terminal_proven=True)
    assert result["claim_released"] is False
    path = Path(result["terminal_receipt"])
    assert path.stat().st_mode & 0o077 == 0
    receipt = custody.read_exit(home, args.card, args.claim_revision)
    assert receipt["invocation"] == "c" * 32 and receipt["exit_code"] == 0
    assert custody.retain_review_exit(home, args, card, terminal_proven=True) == result
    args.production_child_exit_code = 1
    refused = custody.retain_review_exit(home, args, card, terminal_proven=True)
    assert "terminal_receipt" not in refused and refused["claim_released"] is False
    assert json.loads(path.read_text()) == receipt


@pytest.mark.parametrize("changed", ["claim", "owner", "invocation", "host", "exit", "live"])
def test_missing_or_stale_terminal_metadata_retains_claim_without_receipt(
    worker, monkeypatch, changed
):
    home, args, card = worker
    if changed == "claim":
        card.meta["_claim_revision"] = "d" * 32
    elif changed == "owner":
        card.owner = "new-owner"
    elif changed == "invocation":
        monkeypatch.delenv("INVOCATION_ID")
    elif changed == "host":
        args.host = "different"
    elif changed == "exit":
        args.production_child_exit_code = None
    result = custody.retain_review_exit(home, args, card, terminal_proven=changed != "live")
    assert result["claim_released"] is False and "terminal_receipt" not in result
    assert not custody.exit_path(home, args.card, args.claim_revision).exists()


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"LoadState": "not-found", "InvocationID": ""},
        {"ActiveState": "active"},
        {"InvocationID": "d" * 32},
        {"MainPID": "123"},
        {"ControlPID": "123"},
    ],
)
def test_current_unit_must_be_same_terminal_invocation_or_collected(monkeypatch, change):
    values = dict(
        LoadState="loaded",
        ActiveState="inactive",
        SubState="dead",
        MainPID="0",
        ControlPID="0",
        InvocationID="c" * 32,
    )
    values.update(change)
    monkeypatch.setattr(
        custody.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="\n".join(key + "=" + value for key, value in values.items())
        ),
    )
    if not change or change.get("LoadState") == "not-found":
        assert (
            custody.unit_terminal("skfleet-worker-deepseek-ab000002.service", "c" * 32) == values
        )
    else:
        with pytest.raises(ValueError, match="death unproven"):
            custody.unit_terminal("skfleet-worker-deepseek-ab000002.service", "c" * 32)


def test_missing_or_forged_receipt_never_counts_as_terminal(worker):
    home, args, _ = worker
    with pytest.raises(FileNotFoundError):
        custody.read_exit(home, args.card, args.claim_revision)
    path = custody.exit_path(home, args.card, args.claim_revision)
    custody.once(
        path,
        {
            "schema": "model-says-done",
            "card": args.card,
            "claim_revision": args.claim_revision,
            "exit_code": 0,
        },
    )
    with pytest.raises(ValueError):
        custody.read_exit(home, args.card, args.claim_revision)
