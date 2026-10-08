"""The automatic refresh has one durable native qualification slot."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profile
from skcapstone.fleet import production_tests as tests
from skcapstone.fleet import profile_requalification as refresh


def test_only_exact_open_refresh_preserves_its_claim(tmp_path):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    path = root / ("a" * 64 + ".job.json")
    plan.write_once(
        path,
        {
            "schema": "skfleet.profile-requalification/v1",
            "card": "1234abcd",
            "owner": "pi-glm-chiap08-1234abcd",
            "claim_revision": "claim-1",
        },
    )

    assert refresh.retains_pending_claim(
        tmp_path, "1234abcd", "pi-glm-chiap08-1234abcd", "claim-1"
    )
    assert not refresh.retains_pending_claim(
        tmp_path, "1234abcd", "pi-glm-chiap08-1234abcd", "claim-2"
    )

    plan.write_once(
        path.with_name(path.stem + ".failed.json"),
        {"schema": "skfleet.profile-requalification-failure/v1"},
    )
    assert not refresh.retains_pending_claim(
        tmp_path, "1234abcd", "pi-glm-chiap08-1234abcd", "claim-1"
    )


def test_rotation_reaper_preserves_pending_refresh_claim():
    rotate = Path(__file__).resolve().parents[2] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(rotate.read_text(encoding="utf-8"))
    reaper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "reap_dead_claims"
    )

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "retains_pending_claim"
        for node in ast.walk(reaper)
    )


def test_only_one_unfinished_native_refresh_is_advanced(tmp_path):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    first = root / ("a" * 64 + ".job.json")
    second = root / ("b" * 64 + ".job.json")
    plan.write_once(first, {"card": "1234abcd"})
    plan.write_once(second, {"card": "5678abcd"})
    pending = list(refresh._records(root))
    assert len(pending) == 2
    assert pending[0][1]["card"] == "1234abcd"
    plan.write_once(
        pending[0][0].with_name(pending[0][0].stem + ".done.json"),
        {"card": "1234abcd"},
    )
    assert [job["card"] for _, job in refresh._records(root)] == ["5678abcd"]


def test_existing_refresh_keeps_the_single_slot_ahead_of_new_work(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    first = root / ("a" * 64 + ".job.json")
    second = root / ("b" * 64 + ".job.json")
    plan.write_once(first, {"card": "1234abcd"})
    plan.write_once(second, {"card": "5678abcd"})
    advanced = []
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        refresh,
        "_advance",
        lambda home, policy, skc, actor, path, job: advanced.append(job["card"]) or "pending",
    )

    state = refresh.requalify_or_advance(
        tmp_path,
        {"authority_host": "chiap08"},
        "/test/skcapstone",
        "niobe",
        card_id="5678abcd",
        core={"id": "5678abcd"},
        workspace=str(tmp_path),
        owner="producer",
        claim_revision="claim",
    )

    assert state == "busy"
    assert advanced == ["1234abcd"]


def test_stale_plan_appends_current_requalification_successor(tmp_path, monkeypatch):
    binding = {
        "source_card": "1234abcd",
        "source_owner": "producer",
        "source_claim_revision": "claim-1",
        "source_head": "a" * 40,
        "source_tree": "b" * 40,
        "source_revision": "c" * 64,
        "criteria_sha256": "d" * 64,
    }
    profile_value = {
        "qualified_by": "operator",
        "qualification_sha256": "e" * 64,
        "recipe": {"kind": "python"},
    }
    job = {
        "card": "1234abcd",
        "owner": "producer",
        "claim_revision": "claim-1",
        "workspace": str(tmp_path),
        "binding": binding,
        "profile_sha256": "f" * 64,
    }
    card = SimpleNamespace(
        status=SimpleNamespace(value="doing"),
        owner="producer",
        meta={"_claim_revision": "claim-1"},
    )
    monkeypatch.setattr(
        refresh, "CardStore", lambda home: SimpleNamespace(fold=lambda card_id: card)
    )
    monkeypatch.setattr(plan, "source_state", lambda workspace, expected: None)
    monkeypatch.setattr(
        profile,
        "read_profile",
        lambda home, card_id: (profile_value, job["profile_sha256"]),
    )
    previous = {
        "profile_requalification": True,
        "profile_predecessor_sha256": job["profile_sha256"],
    }
    load_calls = []

    def load_plan(home, expected, **kwargs):
        load_calls.append(kwargs)
        if kwargs.get("require_current") is False:
            return previous, tmp_path / "prior-plan.json", "a" * 64
        raise plan.TestEvidenceError("operator test plan is invalid or stale")

    sealed = []
    monkeypatch.setattr(plan, "load_plan", load_plan)
    monkeypatch.setattr(plan, "seal_plan", lambda *args, **kwargs: sealed.append(kwargs))
    monkeypatch.setattr(tests, "run_or_read_tests", lambda *args: None)

    assert refresh._advance(tmp_path, {}, "/test/skcapstone", "niobe", tmp_path / "job", job) == (
        "pending"
    )
    assert load_calls == [{"allow_completed": True}, {"require_current": False}]
    assert sealed == [
        {
            "profile": profile_value,
            "predecessor_sha256": "a" * 64,
            "requalification": True,
            "profile_predecessor_sha256": job["profile_sha256"],
        }
    ]


def test_stale_plan_recovery_does_not_hide_other_plan_errors(tmp_path, monkeypatch):
    binding = {"source_card": "1234abcd"}
    job = {
        "card": "1234abcd",
        "owner": "producer",
        "claim_revision": "claim-1",
        "workspace": str(tmp_path),
        "binding": binding,
    }
    card = SimpleNamespace(
        status=SimpleNamespace(value="doing"),
        owner="producer",
        meta={"_claim_revision": "claim-1"},
    )
    monkeypatch.setattr(
        refresh, "CardStore", lambda home: SimpleNamespace(fold=lambda card_id: card)
    )
    monkeypatch.setattr(plan, "source_state", lambda workspace, expected: None)
    monkeypatch.setattr(
        plan,
        "load_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            plan.TestEvidenceError("test plan chain is disconnected")
        ),
    )

    with pytest.raises(plan.TestEvidenceError, match="test plan chain is disconnected"):
        refresh._advance(tmp_path, {}, "/test/skcapstone", "niobe", tmp_path / "job", job)
def test_harvest_publishes_only_valid_receipt_before_any_new_claim(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    job_path = root / ("a" * 64 + ".job.json")
    job = {
        "schema": "skfleet.profile-requalification/v1",
        "card": "1234abcd",
        "owner": "old-owner",
        "claim_revision": "old-claim",
        "binding": {"source_card": "1234abcd", "criteria_sha256": "b" * 64},
        "workspace": str(tmp_path / "workspace"),
        "profile_sha256": "c" * 64,
        "source_sha256": "d" * 64,
    }
    plan.write_once(job_path, job)
    run = tmp_path / "run"
    run.mkdir()
    (run / "receipt.json").write_text("{}")
    state = {"profile_sha": "c" * 64, "published": False}
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh.plan, "load_plan", lambda *_a, **_k: (
        {"profile_requalification": True, "profile_predecessor_sha256": "c" * 64,
         "runtime_sha256": "runtime", "policy_sha256": "policy"}, tmp_path / "plan", "e" * 64
    ))
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_a: run)
    monkeypatch.setattr(refresh.plan, "runtime_fingerprint", lambda: "runtime")
    monkeypatch.setattr(refresh.plan, "execution_policy_fingerprint", lambda _p: "policy")
    monkeypatch.setattr(refresh.plan, "source_state", lambda *_a: None)
    monkeypatch.setattr(refresh.tests, "validate_test_receipt", lambda *_a: {
        "receipt_sha256": "f" * 64
    })
    monkeypatch.setattr(refresh.profile, "read_profile", lambda *_a, **_k: (
        {"repository": "https://example.invalid/r", "recipe": {"pytest": []},
         "qualified_by": "operator", "qualification_sha256": "old"}, state["profile_sha"]
    ))
    monkeypatch.setattr(refresh.profile, "fingerprint_only_stale", lambda *_a, **_k: True)
    monkeypatch.setattr(refresh.profile, "supersede_profile", lambda *a, **kw: (
        state.update(profile_sha="9" * 64, published=True) or tmp_path / "profile"
    ))
    monkeypatch.setattr(refresh, "CardStore", lambda _home: SimpleNamespace(
        fold=lambda _card: SimpleNamespace(
            status=SimpleNamespace(value="ready"), owner=None,
            model_dump=lambda **_kw: {"id": "1234abcd"},
        )
    ))

    assert (
        refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"})
        == "qualified:1234abcd"
    )
    assert state["published"]
    assert (root / ("a" * 64 + ".job.done.json")).is_file()


def test_harvest_refuses_to_publish_when_card_is_still_owned(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    job_path = root / ("a" * 64 + ".job.json")
    plan.write_once(job_path, {
        "schema": "skfleet.profile-requalification/v1", "card": "1234abcd",
        "binding": {}, "workspace": str(tmp_path),
    })
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh.plan, "load_plan", lambda *_a, **_k: ({}, tmp_path, "e" * 64))
    run = tmp_path / "run"
    run.mkdir()
    (run / "receipt.json").write_text("{}")
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_a: run)
    monkeypatch.setattr(refresh, "CardStore", lambda _home: SimpleNamespace(
        fold=lambda _card: SimpleNamespace(status=SimpleNamespace(value="doing"), owner="other")
    ))

    assert (
        refresh.harvest_completed(
            tmp_path, {"authority_host": "chiap08"}, "1234abcd"
        )
        == "held:card-owned-or-not-ready"
    )


def test_rotate_harvests_before_builder_claim():
    rotate = Path(__file__).resolve().parents[2] / "scripts/fleet/skfleet-rotate.py"
    source = rotate.read_text(encoding="utf-8")
    loop = source.index("for _pick_index")
    assert source.index("_harvest_state = harvest_completed(", loop) < source.index(
        'claim=subprocess.run([SKC,"coord","claim"', loop
    )
