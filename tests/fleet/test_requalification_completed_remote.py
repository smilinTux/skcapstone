"""A remote consumer must validate completed receipts after runtime refreshes."""

from types import SimpleNamespace

import pytest

from skcapstone.fleet import profile_requalification as refresh


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("state", ["complete", "incomplete", "bad-receipt", "wrong-host"])
def test_remote_completion_after_runtime_refresh(tmp_path, monkeypatch, initial, state):
    plan = refresh.plan
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    value = {
        "card": "1234abcd",
        "repository": "https://example.invalid/repo",
        "recipe": {"pytest": {"tests/test_one.py": 1}},
    }
    job = {
        "card": "1234abcd",
        "owner": "niobe-requal-1234abcd",
        "claim_revision": "claim",
        "execution_host": "worker",
        "workspace": str(tmp_path / "source"),
        "binding": {"source_head": "a" * 40},
        "profile_sha256": "b" * 64,
        "source_sha256": "c" * 64,
    }
    value["source_sha256"] = job["source_sha256"]
    sealed = {
        "host": "other" if state == "wrong-host" else "worker",
        "authority_host": "authority",
        "remote_requalification": not initial,
        "remote_initial_profile_qualification": initial,
        "initial_profile_qualification": initial,
        "profile": value,
    }
    if initial:
        job.update(initial_profile=True, initial_profile_value=value)
    path = root / ("d" * 64 + ".job.json")
    plan.write_once(path, job)
    card = SimpleNamespace(
        archived=False,
        status=SimpleNamespace(value="doing"),
        owner=job["owner"],
        meta={"_claim_revision": "claim"},
        model_dump=lambda **kwargs: {},
    )
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(refresh, "CardStore", lambda home: SimpleNamespace(fold=lambda cid: card))
    monkeypatch.setattr("skcapstone.fleet.store.actuation_allowed", lambda paths: True)
    monkeypatch.setattr("skcapstone.fleet.builder_dispatch.materialize_source", lambda *args: None)
    monkeypatch.setattr(plan, "source_state", lambda *args: None)
    monkeypatch.setattr(refresh.profile, "read_profile", lambda *args: (value, "b" * 64))
    monkeypatch.setattr(refresh.profile, "initial_recipe", lambda *args: (value["recipe"], None))

    def load(home, binding, **kwargs):
        # The native loader retains current-runtime refusal until all three
        # completed artifacts exist. Completion is not receipt acceptance.
        if not kwargs.get("allow_completed") or state == "incomplete":
            raise plan.TestEvidenceError("operator test plan is invalid or stale")
        return sealed, tmp_path / "plan.json", "e" * 64

    validated = []

    def read_receipt(*args):
        validated.append(args)
        if state == "bad-receipt":
            raise plan.TestEvidenceError("native unit, source or receipt custody mismatch")
        return {"receipt_sha256": "f" * 64}

    monkeypatch.setattr(plan, "load_plan", load)
    monkeypatch.setattr(refresh.tests, "run_or_read_tests", read_receipt)
    result = refresh.consume_remote(tmp_path, {"authority_host": "authority"}, "worker")
    failed = path.with_name(path.stem + ".failed.json")
    if state == "complete":
        assert result == ["receipt:1234abcd"]
        assert len(validated) == 1
        assert not failed.exists()
    else:
        assert result[0].startswith("failed:1234abcd:")
        reason = plan.read_json(failed)["reason"]
        expected = {
            "incomplete": "operator test plan is invalid or stale",
            "bad-receipt": "native unit, source or receipt custody mismatch",
            "wrong-host": (
                "remote initial qualification plan changed"
                if initial
                else "remote qualification plan binding changed"
            ),
        }
        assert reason == expected[state]
        assert bool(validated) == (state == "bad-receipt")
