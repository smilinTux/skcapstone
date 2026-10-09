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


def test_existing_refresh_does_not_block_another_card_job(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    first = root / ("a" * 64 + ".job.json")
    plan.write_once(first, {"card": "1234abcd"})
    begun = []
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        refresh,
        "_begin",
        lambda home, policy, skc, actor, card_id, *args: begun.append(card_id) or "pending",
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

    assert state == "pending"
    assert begun == ["5678abcd"]


def test_card_specific_refresh_skips_stale_claim_generations(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    for suffix, owner, revision in (
        ("a", "old-owner", "old-revision"),
        ("b", "niobe", "new-revision"),
    ):
        plan.write_once(
            root / (suffix * 64 + ".job.json"),
            {"card": "1234abcd", "owner": owner, "claim_revision": revision},
        )
    advanced = []
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh, "_claim_is_current", lambda *_args: True)
    monkeypatch.setattr(
        refresh,
        "_advance",
        lambda _home, _policy, _skc, _actor, path, _job: advanced.append(path.name) or "pending",
    )

    state = refresh.requalify_or_advance(
        tmp_path,
        {"authority_host": "chiap08"},
        "/test/skcapstone",
        "niobe",
        card_id="1234abcd",
        owner="niobe",
        claim_revision="new-revision",
    )

    assert state == "pending"
    assert advanced == ["b" * 64 + ".job.json"]


def test_authority_advances_four_current_remote_jobs_per_cycle(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    cards = ["1234abcd", "2345bcde", "3456cdef", "4567def0", "5678ef01"]
    for index, card_id in enumerate(cards):
        plan.write_once(
            root / (str(index) * 64 + ".job.json"),
            {
                "card": card_id,
                "owner": "niobe",
                "claim_revision": str(index),
            },
        )
    advanced = []
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh, "_claim_is_current", lambda *_args: True)
    monkeypatch.setattr(
        refresh,
        "_advance",
        lambda _home, _policy, _skc, _actor, _path, job: advanced.append(job["card"]) or "pending",
    )

    state = refresh.requalify_or_advance(
        tmp_path, {"authority_host": "chiap08"}, "/test/skcapstone", "niobe"
    )

    assert advanced == cards[:4]
    assert state == "batch:" + ",".join(f"{card}=pending" for card in cards[:4])


def test_stale_profile_candidate_queues_under_governed_claim(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_profile as test_profile

    (tmp_path / "fleet").mkdir(mode=0o700)
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        test_profile,
        "preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            test_profile.ProfileRequalificationRequired("stale fingerprints")
        ),
    )
    current = {"repository": "https://github.com/example/repo.git"}
    monkeypatch.setattr(test_profile, "read_profile", lambda *_a, **_k: (current, "p" * 64))
    monkeypatch.setattr(test_profile, "contract", lambda _core: {"card": "1234abcd"})
    monkeypatch.setattr(test_profile, "fingerprint_only_stale", lambda *_a, **_k: True)
    monkeypatch.setattr(refresh.plan, "workspace_source_fingerprint", lambda *_a: "s" * 64)
    monkeypatch.setattr(refresh, "_execution_host", lambda *_a: "chiap01")
    card = SimpleNamespace(status=SimpleNamespace(value="ready"), owner=None, meta={})
    monkeypatch.setattr(refresh.CardStore, "fold", lambda *_a: card)
    calls = []

    def claim(argv, **_kwargs):
        calls.append(argv)
        card.status.value = "doing"
        card.owner = "niobe"
        card.meta["_claim_revision"] = "claim-1"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        "subprocess.run",
        claim,
    )
    monkeypatch.setattr(refresh, "requalify_or_advance", lambda *_a, **_kw: "pending")
    monkeypatch.setattr(
        refresh,
        "_records",
        lambda _root: iter(
            [
                (
                    Path("job.job.json"),
                    {
                        "card": "1234abcd",
                        "owner": "niobe",
                        "claim_revision": "claim-1",
                        "execution_host": "chiap01",
                    },
                )
            ]
        ),
    )
    workspace = tmp_path / "exact-source"

    state = refresh.offer_stale_candidate(
        tmp_path,
        {"authority_host": "chiap08"},
        "/test/skcapstone",
        "niobe",
        {"id": "1234abcd"},
        ["source-only", "glm-only", "sk-m"],
        lambda _core, _labels: workspace,
    )

    assert state == "pending:chiap01"
    assert calls == [["/test/skcapstone", "coord", "claim", "1234abcd", "--agent", "niobe"]]


def test_missing_profile_queues_fixed_recipe_as_remote_native_job(tmp_path, monkeypatch):
    (tmp_path / "fleet").mkdir(mode=0o700)
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        profile,
        "preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            plan.TestEvidenceError("required-test-profile-unqualified")
        ),
    )
    core = {
        "id": "1234abcd",
        "description": "Repair src/skcapstone/example.py and run pytest.",
        "acceptance_criteria": ["The full test suite passes."],
        "links": {"repository": "https://github.com/example/repo.git"},
    }
    monkeypatch.setattr(
        profile,
        "contract",
        lambda _core: {
            "card": "1234abcd",
            "repository": "https://github.com/example/repo.git",
            "criteria_sha256": "c" * 64,
        },
    )
    monkeypatch.setattr(profile, "initial_recipe", lambda *_args: ({"pytest_all": True}, None))
    monkeypatch.setattr(refresh.plan, "workspace_source_fingerprint", lambda *_args: "s" * 64)
    monkeypatch.setattr(refresh, "_execution_host", lambda *_args: "chiap01")
    card = SimpleNamespace(status=SimpleNamespace(value="ready"), owner=None, meta={})
    monkeypatch.setattr(refresh.CardStore, "fold", lambda *_args: card)
    calls = []

    def claim(argv, **_kwargs):
        calls.append(argv)
        card.status.value = "doing"
        card.owner = "niobe"
        card.meta["_claim_revision"] = "claim-1"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", claim)
    monkeypatch.setattr(refresh, "requalify_or_advance", lambda *_args, **_kw: "pending")
    monkeypatch.setattr(
        refresh,
        "_records",
        lambda _root: iter(
            [
                (
                    Path("job.job.json"),
                    {
                        "card": "1234abcd",
                        "owner": "niobe",
                        "claim_revision": "claim-1",
                        "execution_host": "chiap01",
                    },
                )
            ]
        ),
    )
    state = refresh.offer_stale_candidate(
        tmp_path,
        {"authority_host": "chiap08"},
        "/test/skcapstone",
        "niobe",
        core,
        ["source-only", "glm-only", "sk-m"],
        lambda _core, _labels: tmp_path / "exact-source",
    )

    assert state == "pending:chiap01"
    assert calls == [["/test/skcapstone", "coord", "claim", "1234abcd", "--agent", "niobe"]]


def test_remote_qualification_uses_least_loaded_ready_node(tmp_path, monkeypatch):
    nodes = [SimpleNamespace(name=name) for name in ("node01", "node02", "node03", "node-wk12")]
    hosts = {
        "node01": "chiap01",
        "node02": "chiap02",
        "node03": "chiap03",
        "node-wk12": "chiwk12",
    }
    pending = [
        (Path("one.job.json"), {"execution_host": "chiap01"}),
        (Path("two.job.json"), {"execution_host": "chiap01"}),
        (Path("three.job.json"), {"execution_host": "chiap02"}),
    ]
    monkeypatch.setattr(refresh, "_records", lambda _root: iter(pending))
    monkeypatch.setattr("skcapstone.fleet.builder_dispatch._ready_builders", lambda _paths: nodes)
    monkeypatch.setattr("skcapstone.fleet.production_builder.ready_nodes", lambda *_a: nodes)
    monkeypatch.setattr(
        "skcapstone.fleet.production_builder.node_binding",
        lambda _paths, node, _policy: {"host": hosts[node]},
    )
    monkeypatch.setattr(
        "skcapstone.fleet.builder_dispatch.production_load_key",
        lambda _paths, view, _policy: view.name,
    )

    assert (
        refresh._execution_host(tmp_path, {"authority_host": "chiap08"}, "1234abcd") == "chiap03"
    )


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
    assert load_calls == [
        {"allow_completed": True, "allow_remote_host": True},
        {"require_current": False, "allow_remote_host": True},
    ]
    assert sealed == [
        {
            "profile": profile_value,
            "predecessor_sha256": "a" * 64,
            "requalification": True,
            "profile_predecessor_sha256": job["profile_sha256"],
            "execution_host": None,
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
    monkeypatch.setattr(
        refresh.plan,
        "load_plan",
        lambda *_a, **_k: (
            {
                "profile_requalification": True,
                "profile_predecessor_sha256": "c" * 64,
                "runtime_sha256": "runtime",
                "policy_sha256": "policy",
            },
            tmp_path / "plan",
            "e" * 64,
        ),
    )
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_a: run)
    monkeypatch.setattr(refresh.plan, "runtime_fingerprint", lambda: "runtime")
    monkeypatch.setattr(refresh.plan, "execution_policy_fingerprint", lambda _p, *_a: "policy")
    monkeypatch.setattr(refresh.plan, "source_state", lambda *_a: None)
    monkeypatch.setattr(
        refresh.tests, "validate_test_receipt", lambda *_a: {"receipt_sha256": "f" * 64}
    )
    monkeypatch.setattr(
        refresh.profile,
        "read_profile",
        lambda *_a, **_k: (
            {
                "repository": "https://example.invalid/r",
                "recipe": {"pytest": []},
                "qualified_by": "operator",
                "qualification_sha256": "old",
            },
            state["profile_sha"],
        ),
    )
    monkeypatch.setattr(refresh.profile, "fingerprint_only_stale", lambda *_a, **_k: True)
    monkeypatch.setattr(
        refresh.profile,
        "supersede_profile",
        lambda *a, **kw: (
            state.update(profile_sha="9" * 64, published=True) or tmp_path / "profile"
        ),
    )
    monkeypatch.setattr(
        refresh,
        "CardStore",
        lambda _home: SimpleNamespace(
            fold=lambda _card: SimpleNamespace(
                status=SimpleNamespace(value="ready"),
                owner=None,
                model_dump=lambda **_kw: {"id": "1234abcd"},
            )
        ),
    )

    assert (
        refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}) == "qualified:1234abcd"
    )
    assert state["published"]
    assert (root / ("a" * 64 + ".job.done.json")).is_file()


def test_harvest_discovers_completed_plan_without_job_record(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    plans = tmp_path / "fleet/test-plans"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    plan.private_dir(plans, create=True)
    workspace = tmp_path / "fleet/workspaces/producer"
    workspace.mkdir(parents=True)
    binding = {
        "source_card": "1234abcd",
        "source_owner": "producer",
        "source_claim_revision": "ended-claim",
        "source_head": "a" * 40,
        "source_tree": "b" * 40,
        "source_revision": "c" * 64,
        "criteria_sha256": "d" * 64,
    }
    plan_path = plans / "1234abcd-completed.json"
    plan.write_once(plan_path, {"binding": binding, "profile_requalification": True})
    run = tmp_path / "run"
    run.mkdir()
    (run / "receipt.json").write_text("{}")
    card = SimpleNamespace(
        status=SimpleNamespace(value="ready"),
        owner=None,
        model_dump=lambda **_kwargs: {"id": "1234abcd"},
    )
    current = {
        "repository": "https://example.invalid/r",
        "recipe": {"pytest": []},
        "qualified_by": "operator",
    }
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        refresh.plan,
        "load_plan",
        lambda *_args, **_kwargs: (
            {
                "profile_requalification": True,
                "profile_predecessor_sha256": "c" * 64,
                "runtime_sha256": "runtime",
                "policy_sha256": "policy",
            },
            plan_path,
            "e" * 64,
        ),
    )
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_args: run)
    monkeypatch.setattr(refresh.plan, "runtime_fingerprint", lambda: "runtime")
    monkeypatch.setattr(
        refresh.plan, "execution_policy_fingerprint", lambda _policy, *_a: "policy"
    )
    monkeypatch.setattr(refresh.plan, "source_state", lambda *_args: None)
    monkeypatch.setattr(refresh.plan, "workspace_source_fingerprint", lambda *_args: "d" * 64)
    monkeypatch.setattr(
        refresh.tests, "validate_test_receipt", lambda *_args: {"receipt_sha256": "f" * 64}
    )
    monkeypatch.setattr(
        refresh.profile, "read_profile", lambda *_args, **_kwargs: (current, "c" * 64)
    )
    monkeypatch.setattr(refresh.profile, "fingerprint_only_stale", lambda *_args, **_kwargs: True)
    published = []
    monkeypatch.setattr(
        refresh.profile,
        "supersede_profile",
        lambda *args, **kwargs: published.append(kwargs) or tmp_path / "profile",
    )
    monkeypatch.setattr(
        refresh, "CardStore", lambda _home: SimpleNamespace(fold=lambda _card: card)
    )

    assert (
        refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}) == "qualified:1234abcd"
    )
    assert published and published[0]["unclaimed"] is True
    assert (root / ("harvested-" + "e" * 64 + ".json")).is_file()


def test_harvest_publishes_only_the_bounded_batch(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    run = tmp_path / "run"
    run.mkdir()
    (run / "receipt.json").write_text("{}")
    cards = ["1234abcd", "2345bcde", "3456cdef", "4567def0", "5678ef01"]
    for index, card_id in enumerate(cards):
        plan.write_once(
            root / (str(index) * 64 + ".job.json"),
            {
                "schema": "skfleet.profile-requalification/v1",
                "card": card_id,
                "binding": {"source_card": card_id, "criteria_sha256": "b" * 64},
                "workspace": str(tmp_path / ("workspace-" + card_id)),
                "profile_sha256": "c" * 64,
                "source_sha256": "d" * 64,
            },
        )
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        refresh.plan,
        "load_plan",
        lambda _home, binding, **_kwargs: (
            {
                "profile_requalification": True,
                "profile_predecessor_sha256": "c" * 64,
                "runtime_sha256": "runtime",
                "policy_sha256": "policy",
            },
            tmp_path / "plan",
            (binding["source_card"][0] * 64),
        ),
    )
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_args: run)
    monkeypatch.setattr(refresh.plan, "runtime_fingerprint", lambda: "runtime")
    monkeypatch.setattr(refresh.plan, "execution_policy_fingerprint", lambda *_a: "policy")
    monkeypatch.setattr(refresh.plan, "source_state", lambda *_args: None)
    monkeypatch.setattr(refresh.plan, "workspace_source_fingerprint", lambda *_args: "d" * 64)
    monkeypatch.setattr(
        refresh.tests, "validate_test_receipt", lambda *_args: {"receipt_sha256": "f" * 64}
    )
    monkeypatch.setattr(
        refresh.profile,
        "read_profile",
        lambda _home, card_id: (
            {
                "repository": "https://example.invalid/r",
                "recipe": {"pytest": []},
                "qualified_by": "operator",
            },
            "c" * 64,
        ),
    )
    monkeypatch.setattr(refresh.profile, "fingerprint_only_stale", lambda *_a, **_k: True)
    published = []
    monkeypatch.setattr(
        refresh.profile,
        "supersede_profile",
        lambda _home, card, *_args, **_kwargs: published.append(card["id"]),
    )
    monkeypatch.setattr(
        refresh,
        "CardStore",
        lambda _home: SimpleNamespace(
            fold=lambda card_id: SimpleNamespace(
                status=SimpleNamespace(value="ready"),
                owner=None,
                model_dump=lambda **_kwargs: {"id": card_id},
            )
        ),
    )

    state = refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}, limit=3)

    assert state == "qualified:" + ",".join(cards[:3])
    assert published == cards[:3]


def test_harvest_does_not_publish_orphan_plan_with_invalid_receipt(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    plans = tmp_path / "fleet/test-plans"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    plan.private_dir(plans, create=True)
    binding = {"source_card": "1234abcd", "source_owner": "producer"}
    plan.write_once(
        plans / "1234abcd-invalid.json",
        {"binding": binding, "profile_requalification": True},
    )
    card = SimpleNamespace(
        status=SimpleNamespace(value="ready"),
        owner=None,
        model_dump=lambda **_kwargs: {"id": "1234abcd"},
    )
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        refresh.plan,
        "load_plan",
        lambda *_args, **_kwargs: (
            {
                "profile_requalification": True,
                "profile_predecessor_sha256": "c" * 64,
                "runtime_sha256": "runtime",
                "policy_sha256": "policy",
            },
            plans / "plan.json",
            "e" * 64,
        ),
    )
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_args: plans)
    monkeypatch.setattr(refresh.plan, "runtime_fingerprint", lambda: "runtime")
    monkeypatch.setattr(
        refresh.plan, "execution_policy_fingerprint", lambda _policy, *_a: "policy"
    )
    monkeypatch.setattr(refresh.plan, "source_state", lambda *_args: None)
    (plans / "receipt.json").write_text("{}")
    validated = []
    monkeypatch.setattr(
        refresh.tests,
        "validate_test_receipt",
        lambda *_args: (
            validated.append(True)
            or (_ for _ in ()).throw(plan.TestEvidenceError("receipt custody invalid"))
        ),
    )
    monkeypatch.setattr(
        refresh.profile, "read_profile", lambda *_args, **_kwargs: ({"repository": "r"}, "c" * 64)
    )
    monkeypatch.setattr(refresh.profile, "fingerprint_only_stale", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        refresh,
        "CardStore",
        lambda _home: SimpleNamespace(fold=lambda _card: card),
    )
    published = []
    monkeypatch.setattr(
        refresh.profile, "supersede_profile", lambda *args, **kwargs: published.append(args)
    )

    assert refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}) == "idle"
    assert validated
    assert not published


def test_harvest_refuses_to_publish_when_card_is_still_owned(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    job_path = root / ("a" * 64 + ".job.json")
    plan.write_once(
        job_path,
        {
            "schema": "skfleet.profile-requalification/v1",
            "card": "1234abcd",
            "binding": {},
            "workspace": str(tmp_path),
        },
    )
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh.plan, "load_plan", lambda *_a, **_k: ({}, tmp_path, "e" * 64))
    run = tmp_path / "run"
    run.mkdir()
    (run / "receipt.json").write_text("{}")
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_a: run)
    monkeypatch.setattr(
        refresh,
        "CardStore",
        lambda _home: SimpleNamespace(
            fold=lambda _card: SimpleNamespace(
                status=SimpleNamespace(value="doing"), owner="other"
            )
        ),
    )

    assert (
        refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}, "1234abcd")
        == "held:card-owned-or-not-ready"
    )


def test_rotate_harvests_before_builder_claim():
    rotate = Path(__file__).resolve().parents[2] / "scripts/fleet/skfleet-rotate.py"
    source = rotate.read_text(encoding="utf-8")
    loop = source.index("for _pick_index")
    assert source.index("_harvest_state = harvest_completed(", loop) < source.index(
        'claim=subprocess.run([SKC,"coord","claim"', loop
    )
    requalify = source.index('log(d,"PROFILE_REQUALIFICATION|%s|%s|state=%s"', loop)
    launch = source.index("if PRODUCTION_POLICY and _review_seat is None", requalify)
    deferred = source[requalify:launch]
    assert "BUILDER_CLAIM_NOT_LAUNCHED|" in deferred
    assert '"coord","release-claim"' not in deferred


def test_execution_host_reads_the_fleet_tree_under_the_sovereign_home(tmp_path, monkeypatch):
    """Callers pass ~/.skcapstone; nodes must be read from ~/.skcapstone/fleet."""
    seen = []
    monkeypatch.setattr(refresh, "_records", lambda _root: iter(()))

    def ready(paths):
        seen.append(paths.root)
        return []

    monkeypatch.setattr("skcapstone.fleet.builder_dispatch._ready_builders", ready)
    monkeypatch.setattr("skcapstone.fleet.production_builder.ready_nodes", lambda *_a: [])
    sovereign = tmp_path / ".skcapstone"
    refresh._execution_host(sovereign, {"authority_host": "chiap08"}, "1234abcd")
    assert seen == [sovereign / "fleet"]


def test_consume_remote_honours_a_freeze_under_the_sovereign_home(tmp_path, monkeypatch):
    sovereign = tmp_path / ".skcapstone"
    checked = []
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap01")
    monkeypatch.setattr(refresh.plan, "private_dir", lambda *_a, **_k: None)

    def allowed(paths):
        checked.append(paths.root)
        return False

    monkeypatch.setattr("skcapstone.fleet.store.actuation_allowed", allowed)
    assert refresh.consume_remote(sovereign, {"authority_host": "chiap08"}, "chiap01") == [
        "frozen"
    ]
    assert checked == [sovereign / "fleet"]


def test_failed_qualification_release_returns_the_card_to_ready(tmp_path, monkeypatch):
    """release-claim lands in backlog; offers only see READY, so a failure parked cards."""
    (tmp_path / "fleet").mkdir(mode=0o700)
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        profile,
        "preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            plan.TestEvidenceError("required-test-profile-unqualified")
        ),
    )
    monkeypatch.setattr(
        profile,
        "contract",
        lambda _core: {
            "card": "1234abcd",
            "repository": "https://github.com/example/repo.git",
            "criteria_sha256": "c" * 64,
        },
    )
    monkeypatch.setattr(profile, "initial_recipe", lambda *_args: ({"pytest_all": True}, None))
    monkeypatch.setattr(refresh.plan, "workspace_source_fingerprint", lambda *_args: "s" * 64)
    monkeypatch.setattr(refresh, "_execution_host", lambda *_args: "chiap01")
    card = SimpleNamespace(status=SimpleNamespace(value="ready"), owner=None, meta={})
    monkeypatch.setattr(refresh.CardStore, "fold", lambda *_args: card)
    calls = []

    def coord(argv, **_kwargs):
        calls.append(argv[1:4])
        if argv[2] == "claim":
            card.status.value, card.owner = "doing", "niobe"
            card.meta["_claim_revision"] = "claim-1"
        elif argv[2] == "release-claim":
            card.status.value, card.owner = "backlog", None
        elif argv[2] == "move":
            card.status.value = argv[4]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", coord)
    monkeypatch.setattr(
        refresh, "requalify_or_advance", lambda *_args, **_kw: "failed:runtime differs"
    )
    state = refresh.offer_stale_candidate(
        tmp_path,
        {"authority_host": "chiap08"},
        "/test/skcapstone",
        "niobe",
        {"id": "1234abcd"},
        ["source-only"],
        lambda _core, _labels: tmp_path / "exact-source",
    )
    assert state == "failed:runtime differs"
    assert ["coord", "release-claim", "1234abcd"] in calls
    assert calls[-1] == ["coord", "move", "1234abcd"]
    assert card.status.value == "ready" and card.owner is None
