"""Synthetic native custody and immutable qualification generation boundaries."""

# ruff: noqa: F811

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profile
from tests.fleet.test_production_test_profile import qualified  # noqa: F401
from tests.fleet.test_production_tests import setup  # noqa: F401

CLAIM = {"owner": "producer", "claim_revision": "generation-one"}


def custody(home, core):
    """Create only a temporary native card for the synthetic test."""
    store = CardStore(home)
    store.create(
        CardCore(
            **core,
            title="Synthetic profile generation fixture",
            initial_owner=CLAIM["owner"],
            initial_claim_revision=CLAIM["claim_revision"],
        )
    )
    return store


def successor(qualified, **changes):
    """Publish through the operator API using exact synthetic custody."""
    home, core, policy, recipe, original = qualified
    kwargs = dict(
        predecessor_sha256=plan.sha(original.read_bytes()),
        source_claim=CLAIM,
        runtime_sha256="a" * 64,
    )
    kwargs.update(changes)
    return profile.supersede_profile(home, core, policy, recipe, "operator", "c" * 64, **kwargs)


def test_immutable_predecessor_and_multiple_successors(qualified):
    home, core, policy, _, original = qualified
    custody(home, core)
    before = original.read_bytes()
    first = successor(qualified)
    first_bytes = first.read_bytes()
    value, fingerprint = profile.read_profile(home, core["id"])
    assert value["qualification_sha256"] == "c" * 64
    assert fingerprint == plan.sha(first_bytes)
    assert profile.preflight(home, core, ["source-only"], policy) == value
    second = profile.supersede_profile(
        home,
        core,
        policy,
        qualified[3],
        "operator",
        "d" * 64,
        predecessor_sha256=fingerprint,
        source_claim=CLAIM,
        runtime_sha256="a" * 64,
    )
    assert second.exists()
    assert original.read_bytes() == before
    assert first.read_bytes() == first_bytes
    assert profile.read_profile(home, core["id"], pinned=json.loads(before))[0] == (
        json.loads(before)
    )


def test_unclaimed_backlog_successor_preserves_history(qualified):
    home, core, policy, recipe, original = qualified
    CardStore(home).create(CardCore(**core, title="Unclaimed producer fixture"))
    before = original.read_bytes()
    path = profile.supersede_profile(
        home,
        core,
        policy,
        recipe,
        "operator",
        "c" * 64,
        predecessor_sha256=plan.sha(before),
        runtime_sha256="a" * 64,
        unclaimed=True,
    )
    envelope = json.loads(path.read_bytes())
    assert envelope["schema"] == "skfleet.test-profile-successor/v2"
    assert len(envelope["source_card_sha256"]) == 64
    assert (
        profile.preflight(home, core, ["source-only"], policy)["qualification_sha256"] == "c" * 64
    )
    assert profile.read_profile(home, core["id"], pinned=json.loads(before))[0] == json.loads(
        before
    )
    assert original.read_bytes() == before


def test_unclaimed_successor_refuses_owned_source(qualified):
    home, core, policy, recipe, original = qualified
    custody(home, core)
    with pytest.raises(plan.TestEvidenceError, match="source claim changed"):
        profile.supersede_profile(
            home,
            core,
            policy,
            recipe,
            "operator",
            "c" * 64,
            predecessor_sha256=plan.sha(original.read_bytes()),
            runtime_sha256="a" * 64,
            unclaimed=True,
        )
    assert not (original.parent / core["id"]).exists()


@pytest.mark.parametrize("change", ["hash", "owner", "claim", "runtime", "released", "criteria"])
def test_stale_bindings_write_nothing(qualified, change):
    home, core, _, _, original = qualified
    store = custody(home, core)
    before = original.read_bytes()
    kwargs = {}
    if change == "hash":
        kwargs["predecessor_sha256"] = "f" * 64
    elif change in {"owner", "claim"}:
        kwargs["source_claim"] = dict(
            CLAIM, **{"owner" if change == "owner" else "claim_revision": "wrong"}
        )
    elif change == "runtime":
        kwargs["runtime_sha256"] = "f" * 64
    elif change == "released":
        store.append_event(
            core["id"],
            "release_claim",
            CLAIM["owner"],
            released_owner=CLAIM["owner"],
            expected_claim_revision=CLAIM["claim_revision"],
        )
    else:
        core["acceptance_criteria"] = ["Changed contract"]
    with pytest.raises(plan.TestEvidenceError):
        successor(qualified, **kwargs)
    assert original.read_bytes() == before
    assert not (original.parent / core["id"]).exists()


def test_competing_successors_have_one_winner(qualified):
    custody(qualified[0], qualified[1])

    def attempt(_):
        try:
            successor(qualified)
            return "written"
        except plan.TestEvidenceError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ["refused", "written"]


@pytest.mark.parametrize(
    "damage", ["wrong_hash", "orphan", "missing", "json", "symlink", "profile"]
)
def test_corrupt_chain_refuses_even_historical_pin(qualified, damage):
    home, core, policy, _, original = qualified
    custody(home, core)
    pinned = json.loads(original.read_bytes())
    path = successor(qualified)
    if damage == "wrong_hash":
        envelope = json.loads(path.read_bytes())
        envelope["predecessor_sha256"] = "f" * 64
        path.write_text(json.dumps(envelope))
    elif damage == "orphan":
        plan.write_once(path.parent / ("f" * 64 + ".json"), {})
    elif damage == "missing":
        path.rename(path.parent / ("f" * 64 + ".json"))
    elif damage == "json":
        path.write_text("{")
    elif damage == "profile":
        envelope = json.loads(path.read_bytes())
        envelope["profile"] = {"card": core["id"]}
        path.write_text(json.dumps(envelope))
    else:
        path.unlink()
        path.symlink_to(original)
    with pytest.raises((ValueError, OSError)):
        profile.preflight(home, core, ["source-only"], policy)
    with pytest.raises((ValueError, OSError)):
        profile.read_profile(home, core["id"], pinned=pinned)


def test_sealed_plan_keeps_old_pin_and_refuses_changed_runtime(setup, monkeypatch):  # noqa: F811
    s = setup
    s.plan_path.unlink()
    # The shared admission fixture already owns its own immutable source claim.
    core = {
        "id": "89508f84",
        "meta": {"repository": "https://example.org/public.git"},
        "acceptance_criteria": ["Run parser checks"],
    }
    custody(s.home, core)
    binding = dict(
        s.binding,
        source_card=core["id"],
        source_owner=CLAIM["owner"],
        source_claim_revision=CLAIM["claim_revision"],
        criteria_sha256=profile.contract(core)["criteria_sha256"],
    )
    recipe = {"pytest": {"tests/test_parser.py": 1}, "compile": [], "lint": [], "changelog": False}
    original = profile.qualify_profile(s.home, core, s.policy, recipe, "operator", "b" * 64)
    profile.seal_candidate(s.home, binding, s.workspace, s.policy, core["meta"]["repository"])
    sealed, path, fingerprint = plan.load_plan(s.home, binding)
    profile.supersede_profile(
        s.home,
        core,
        s.policy,
        recipe,
        "operator",
        "c" * 64,
        predecessor_sha256=plan.sha(original.read_bytes()),
        source_claim=CLAIM,
        runtime_sha256=plan.runtime_fingerprint(),
    )
    assert plan.load_plan(s.home, binding)[0] == sealed
    assert plan.sha(path.read_bytes()) == fingerprint
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "f" * 64)
    with pytest.raises(plan.TestEvidenceError, match="stale"):
        plan.load_plan(s.home, binding)
    assert plan.sha(path.read_bytes()) == fingerprint

    _, current_sha = profile.read_profile(s.home, core["id"])
    profile.supersede_profile(
        s.home,
        core,
        s.policy,
        recipe,
        "operator",
        "d" * 64,
        predecessor_sha256=current_sha,
        source_claim=CLAIM,
        runtime_sha256="f" * 64,
    )
    profile.seal_candidate(s.home, binding, s.workspace, s.policy, core["meta"]["repository"])
    latest, latest_path, latest_sha = plan.load_plan(s.home, binding)
    assert latest["schema"] == "skfleet.native-test-plan/v2"
    assert latest["predecessor_sha256"] == fingerprint
    assert latest["profile"]["qualification_sha256"] == "d" * 64
    assert latest_path != path and latest_sha != fingerprint
    assert latest_path.parent == path.parent
    assert plan.sha(path.read_bytes()) == fingerprint
    profile.seal_candidate(s.home, binding, s.workspace, s.policy, core["meta"]["repository"])
    assert plan.load_plan(s.home, binding)[2] == latest_sha


def test_launched_prior_plan_cannot_be_superseded(setup, monkeypatch):  # noqa: F811
    s = setup
    s.plan_path.unlink()
    core = {
        "id": "89508f84",
        "meta": {"repository": "https://example.org/public.git"},
        "acceptance_criteria": ["Run parser checks"],
    }
    custody(s.home, core)
    binding = dict(
        s.binding,
        source_card=core["id"],
        source_owner=CLAIM["owner"],
        source_claim_revision=CLAIM["claim_revision"],
        criteria_sha256=profile.contract(core)["criteria_sha256"],
    )
    recipe = {"pytest": {"tests/test_parser.py": 1}, "compile": [], "lint": [], "changelog": False}
    original = profile.qualify_profile(s.home, core, s.policy, recipe, "operator", "b" * 64)
    profile.seal_candidate(s.home, binding, s.workspace, s.policy, core["meta"]["repository"])
    _, path, fingerprint = plan.load_plan(s.home, binding)
    run = plan.run_directory(s.home, fingerprint)
    plan.private_dir(run.parent, create=True)
    plan.private_dir(run, create=True)
    plan.write_once(run / "launch.json", {"source": "fixture launched"})
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "f" * 64)
    profile.supersede_profile(
        s.home,
        core,
        s.policy,
        recipe,
        "operator",
        "c" * 64,
        predecessor_sha256=plan.sha(original.read_bytes()),
        source_claim=CLAIM,
        runtime_sha256="f" * 64,
    )
    with pytest.raises(plan.TestEvidenceError, match="already launched"):
        profile.seal_candidate(s.home, binding, s.workspace, s.policy, core["meta"]["repository"])
    assert path.exists()
    assert not list(path.parent.glob(path.stem + ".*.json"))


def test_completed_plan_can_be_verified_after_runtime_change(setup, monkeypatch):  # noqa: F811
    s = setup
    run = plan.run_directory(s.home, s.digest)
    plan.private_dir(run.parent, create=True)
    plan.private_dir(run, create=True)
    for name in ("launch.json", "receipt.json", "terminal.json"):
        plan.write_once(run / name, {"fixture": name})
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "f" * 64)
    with pytest.raises(plan.TestEvidenceError, match="stale"):
        plan.load_plan(s.home, s.binding)
    assert plan.load_plan(s.home, s.binding, allow_completed=True)[2] == s.digest
    profile.seal_candidate(s.home, s.binding, s.workspace, s.policy, "https://example.org/repo")
    (run / "receipt.json").unlink()
    with pytest.raises(plan.TestEvidenceError, match="stale"):
        plan.load_plan(s.home, s.binding, allow_completed=True)


def test_policy_change_requires_requalification_before_plan_successor(setup):  # noqa: F811
    s = setup
    s.plan_path.unlink()
    core = {
        "id": "89508f84",
        "meta": {"repository": "https://example.org/public.git"},
        "acceptance_criteria": ["Run parser checks"],
    }
    custody(s.home, core)
    binding = dict(
        s.binding,
        source_card=core["id"],
        source_owner=CLAIM["owner"],
        source_claim_revision=CLAIM["claim_revision"],
        criteria_sha256=profile.contract(core)["criteria_sha256"],
    )
    recipe = {"pytest": {"tests/test_parser.py": 1}, "compile": [], "lint": [], "changelog": False}
    original = profile.qualify_profile(s.home, core, s.policy, recipe, "operator", "b" * 64)
    profile.seal_candidate(s.home, binding, s.workspace, s.policy, core["meta"]["repository"])
    _, path, fingerprint = plan.load_plan(s.home, binding)
    changed_policy = dict(s.policy, remote_review={"enabled": True})
    with pytest.raises(plan.TestEvidenceError, match="profile"):
        profile.seal_candidate(
            s.home, binding, s.workspace, changed_policy, core["meta"]["repository"]
        )
    assert not list(path.parent.glob(path.stem + ".*.json"))
    profile.supersede_profile(
        s.home,
        core,
        changed_policy,
        recipe,
        "operator",
        "c" * 64,
        predecessor_sha256=plan.sha(original.read_bytes()),
        source_claim=CLAIM,
        runtime_sha256=plan.runtime_fingerprint(),
    )
    profile.seal_candidate(
        s.home, binding, s.workspace, changed_policy, core["meta"]["repository"]
    )
    latest, _, _ = plan.load_plan(s.home, binding)
    assert latest["predecessor_sha256"] == fingerprint
    assert latest["policy_sha256"] == profile.digest(changed_policy)


def test_runtime_successor_restores_preflight_without_repinning(qualified, monkeypatch):
    home, core, policy, _, original = qualified
    custody(home, core)
    old = original.read_bytes()
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "d" * 64)
    with pytest.raises(plan.TestEvidenceError, match="environment changed"):
        profile.preflight(home, core, ["source-only"], policy)
    successor(qualified, runtime_sha256="d" * 64)
    assert profile.preflight(home, core, ["source-only"], policy)["runtime_sha256"] == "d" * 64
    assert original.read_bytes() == old


def test_old_evidence_cannot_be_relabelled(qualified):
    home, core, policy, recipe, original = qualified
    custody(home, core)
    with pytest.raises(plan.TestEvidenceError, match="fresh qualification"):
        profile.supersede_profile(
            home,
            core,
            policy,
            recipe,
            "operator",
            "b" * 64,
            predecessor_sha256=plan.sha(original.read_bytes()),
            source_claim=CLAIM,
            runtime_sha256="a" * 64,
        )
    assert not (original.parent / core["id"]).exists()
