"""The automatic refresh has one durable native qualification slot."""

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

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
        card.owner = "niobe-requal-1234abcd"
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
                        "owner": "niobe-requal-1234abcd",
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
    assert calls == [
        ["/test/skcapstone", "coord", "claim", "1234abcd", "--agent", "niobe-requal-1234abcd"]
    ]


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
        card.owner = "niobe-requal-1234abcd"
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
                        "owner": "niobe-requal-1234abcd",
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
    assert calls == [
        ["/test/skcapstone", "coord", "claim", "1234abcd", "--agent", "niobe-requal-1234abcd"]
    ]


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


def test_exact_source_reconstruction_failure_falls_back_to_authority(tmp_path):
    root = tmp_path / "fleet/profile-requalifications"
    root.mkdir(parents=True, mode=0o700)
    job_path = root / ("a" * 64 + ".job.json")
    plan.write_once(
        job_path,
        {
            "card": "1234abcd",
            "source_sha256": "s" * 64,
            "execution_host": "chiap03",
        },
    )
    plan.write_once(
        root / ("a" * 64 + ".job.failed.json"),
        {"reason": "exact source reconstruction failed"},
    )

    assert (
        refresh._execution_host(tmp_path, {"authority_host": "chiap08"}, "1234abcd", "s" * 64)
        == "chiap08"
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
    plan.load_plan(tmp_path, {"source_card": "other"}, require_current=False)
    assert load_calls[-1] == {"require_current": False}
    assert sealed == [
        {
            "profile": profile_value,
            "predecessor_sha256": "a" * 64,
            "requalification": True,
            "profile_predecessor_sha256": job["profile_sha256"],
            "execution_host": None,
        }
    ]


def test_initial_profile_recovers_remote_preflight_failure_with_successor(tmp_path, monkeypatch):
    binding = {"source_card": "1234abcd"}
    value = {
        "qualified_by": "lumina-nor",
        "qualification_sha256": "e" * 64,
        "recipe": {"pytest_all": True},
    }
    job = {
        "card": "1234abcd",
        "owner": "producer",
        "claim_revision": "claim-1",
        "workspace": str(tmp_path),
        "binding": binding,
        "execution_host": "chiap08",
        "initial_profile": True,
        "initial_profile_value": value,
    }
    card = SimpleNamespace(
        status=SimpleNamespace(value="doing"),
        owner="producer",
        meta={"_claim_revision": "claim-1"},
    )
    current_plan = {
        "initial_profile_qualification": True,
        "remote_initial_profile_qualification": True,
        "profile": value,
        "host": "chiap03",
    }
    monkeypatch.setattr(
        refresh, "CardStore", lambda _home: SimpleNamespace(fold=lambda _card_id: card)
    )
    monkeypatch.setattr(plan, "source_state", lambda *_args: None)
    load_calls = []

    def load_plan(_home, _binding, **kwargs):
        load_calls.append(kwargs)
        return current_plan, tmp_path / "plan.json", "a" * 64

    monkeypatch.setattr(plan, "load_plan", load_plan)
    run = tmp_path / "run"
    run.mkdir()
    (run / "launch.json").write_text("{}")
    monkeypatch.setattr(plan, "run_directory", lambda *_args: run)
    monkeypatch.setattr(refresh, "_remote_initial_preflight_failure", lambda *_args: True)
    sealed = []

    def seal_plan(*_args, **kwargs):
        sealed.append(kwargs)
        plan.load_plan(tmp_path, binding, require_current=False)

    monkeypatch.setattr(plan, "seal_plan", seal_plan)
    monkeypatch.setattr(tests, "run_or_read_tests", lambda *_args: None)

    assert (
        refresh._advance(
            tmp_path, {"authority_host": "chiap08"}, "/skcapstone", "jarvis", None, job
        )
        == "pending"
    )
    assert sealed == [
        {
            "profile": value,
            "predecessor_sha256": "a" * 64,
            "execution_host": "chiap08",
            "initial_profile_qualification": True,
            "acceptance_requalification": True,
        }
    ]


def test_remote_initial_preflight_failure_is_recoverable_only_before_test_output(
    tmp_path, monkeypatch
):
    home = tmp_path
    binding = {
        "source_card": "1234abcd",
        "source_head": "head",
        "source_revision": "revision",
    }
    plan_path = home / "fleet/test-plans/plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.parent.chmod(0o700)
    plan_path.write_text("{}")
    plan_path.chmod(0o600)
    initial_path = plan_path.with_name("plan.initial.json")
    initial_path.write_text(
        json.dumps(
            {
                "remote_initial_profile_qualification": True,
                "authority_host": "chiap08",
                "host": "chiap02",
            }
        )
    )
    initial_path.chmod(0o600)
    run = home / "fleet/test-runs" / ("a" * 64)
    run.parent.mkdir(parents=True, exist_ok=True)
    run.parent.chmod(0o700)
    run.mkdir(parents=True)
    run.chmod(0o700)
    (run / ".run.lock").touch()
    (run / "launch.json").write_text(
        json.dumps(
            {
                "binding": binding,
                "request_id": "a" * 64,
                "workspace": str(home),
                "unit": "skfleet-builder-1234abcd-test.service",
                "service_argv": ["systemd-run", "--", "worker"],
            }
        )
    )
    (run / "launch.json").chmod(0o600)
    (run / "terminal.json").write_text(
        json.dumps(
            {
                "unit": "skfleet-builder-1234abcd-test.service",
                "ActiveState": "failed",
                "SubState": "failed",
                "ExecMainStatus": "1",
                "InvocationID": "b" * 32,
            }
        )
    )
    (run / "terminal.json").chmod(0o600)
    monkeypatch.setattr(refresh.plan, "_initial_plan_base", lambda *_args: initial_path)
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_args: run)
    monkeypatch.setattr(refresh.plan, "source_state", lambda *_args: None)
    monkeypatch.setattr(
        refresh.plan,
        "load_plan",
        lambda *_args, **kwargs: (
            (
                {"initial_profile_qualification": True, "host": "chiap08"},
                plan_path,
                "a" * 64,
            )
            if kwargs.get("allow_remote_host")
            else (_ for _ in ()).throw(
                plan.TestEvidenceError("operator test plan is invalid or stale")
            )
        ),
    )
    monkeypatch.setattr(tests, "service_argv", lambda *_args: ["systemd-run", "--", "worker"])

    assert refresh._remote_initial_preflight_failure(home, binding, "a" * 64)
    (run / "unexpected.log").write_text("test output means this was not a preflight-only failure")
    assert not refresh._remote_initial_preflight_failure(home, binding, "a" * 64)


def test_remote_initial_worker_uses_scoped_wrapper(tmp_path, monkeypatch):
    home = tmp_path
    binding = {
        "source_card": "1234abcd",
        "source_head": "head",
        "source_revision": "revision",
    }
    initial_path = home / "fleet/test-plans/plan.initial.json"
    initial_path.parent.mkdir(parents=True)
    initial_path.parent.chmod(0o700)
    initial_path.write_text(json.dumps({"remote_initial_profile_qualification": True}))
    initial_path.chmod(0o600)
    plan_path = initial_path.with_name("plan.initial.next.json")
    run = home / "fleet/test-runs" / ("a" * 64)
    original = [
        "systemd-run",
        "--",
        str(tests.PREFIX / "bin/python"),
        "-I",
        "-m",
        "skcapstone.fleet.production_test_worker",
        str(plan_path),
        str(run),
    ]
    monkeypatch.setattr(refresh.plan, "_initial_plan_base", lambda *_args: initial_path)
    monkeypatch.setattr(tests, "service_argv", lambda *_args: list(original))

    with refresh._remote_initial_worker(home, binding):
        assert refresh._REMOTE_INITIAL_WORKER.get() is not None
        assert tests.service_argv is not None
        actual = tests.service_argv({"binding": binding}, plan_path, run, home)
    assert actual[:-5] == original[:-5]
    assert actual[4:7] == ["-m", "skcapstone.fleet.remote_initial_test_worker", str(plan_path)]


def test_only_retries_exact_remote_predecessor_failure_once(tmp_path):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    binding = {"source_card": "1234abcd"}
    job = {
        "schema": "skfleet.profile-requalification/v1",
        "card": "1234abcd",
        "claim_revision": "claim-1",
        "binding": binding,
        "initial_profile": True,
        "plan_predecessor_sha256": "a" * 64,
    }
    path = root / ("b" * 64 + ".job.json")
    plan.write_once(path, job)
    plan.write_once(
        path.with_name(path.stem + ".failed.json"),
        {
            "schema": "skfleet.profile-requalification-failure/v1",
            "card": "1234abcd",
            "reason": "operator test plan is invalid or stale",
        },
    )

    assert refresh._remote_predecessor_retry_needed(root, job)
    assert not refresh._remote_predecessor_retry_needed(root, dict(job, claim_revision="other"))
    assert not refresh._remote_predecessor_retry_needed(
        root, dict(job, plan_predecessor_sha256="c" * 64)
    )
    retry = dict(job, remote_predecessor_recovery=True)
    retry_id = plan.sha(json.dumps(retry, sort_keys=True, separators=(",", ":")).encode())
    retry_path = root / (retry_id + ".job.json")
    plan.write_once(retry_path, retry)
    plan.write_once(
        retry_path.with_name(retry_path.stem + ".failed.json"),
        {
            "schema": "skfleet.profile-requalification-failure/v1",
            "card": "1234abcd",
            "reason": "operator test plan is invalid or stale",
        },
    )
    assert not refresh._remote_predecessor_retry_needed(root, job)


def test_failed_initial_job_retries_only_for_changed_qualification_code(tmp_path, monkeypatch):
    root = tmp_path / "fleet"
    root.mkdir(mode=0o700)
    card = SimpleNamespace(
        status=SimpleNamespace(value="doing"),
        owner="pi-glm-chiap08-1234abcd",
        meta={"_claim_revision": "claim-1"},
    )
    monkeypatch.setattr(
        refresh, "CardStore", lambda _home: SimpleNamespace(fold=lambda _card_id: card)
    )
    monkeypatch.setattr(refresh, "card_revision", lambda _card: "source-revision")
    monkeypatch.setattr(
        refresh,
        "_git",
        lambda _workspace, *args: (
            "head" if args[0] == "rev-parse" and args[-1] == "HEAD" else "tree"
        ),
    )
    monkeypatch.setattr(plan, "source_state", lambda *_args: None)
    monkeypatch.setattr(plan, "workspace_source_fingerprint", lambda *_args: "s" * 64)
    monkeypatch.setattr(
        profile,
        "contract",
        lambda _core: {
            "criteria_sha256": "c" * 64,
            "repository": "https://github.com/example/repo.git",
        },
    )
    monkeypatch.setattr(
        profile, "read_profile", lambda *_args: (_ for _ in ()).throw(FileNotFoundError())
    )
    monkeypatch.setattr(profile, "initial_recipe", lambda *_args: ({"pytest_all": True}, None))
    monkeypatch.setattr(
        profile,
        "profile_value",
        lambda *_args, **_kwargs: {
            "card": "1234abcd",
            "qualified_by": "niobe",
            "recipe": {"pytest_all": True},
            "qualification_sha256": "q" * 64,
        },
    )
    monkeypatch.setattr(refresh, "_execution_host", lambda *_args: "chiap01")
    monkeypatch.setattr(refresh, "_remote_source_reconstruction_failed", lambda *_args: False)
    monkeypatch.setattr(
        plan,
        "load_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(refresh, "_advance", lambda *_args: "pending")
    code = ["a" * 64]
    monkeypatch.setattr(
        refresh, "_qualification_implementation_sha256", lambda: code[0], raising=False
    )
    args = (
        tmp_path,
        {"authority_host": "chiap08"},
        "/test/skcapstone",
        "niobe",
        "1234abcd",
        {"id": "1234abcd"},
        tmp_path,
        "pi-glm-chiap08-1234abcd",
        "claim-1",
    )

    assert refresh._begin(*args) == "pending"
    first = next((tmp_path / "fleet/profile-requalifications").glob("*.job.json"))
    plan.write_once(
        first.with_name(first.stem + ".failed.json"),
        {"schema": "skfleet.profile-requalification-failure/v1", "reason": "stale plan"},
    )
    code[0] = "b" * 64
    assert refresh._begin(*args) == "pending"
    jobs = list((tmp_path / "fleet/profile-requalifications").glob("*.job.json"))
    assert len(jobs) == 2
    assert {
        json.loads(path.read_text())["qualification_implementation_sha256"] for path in jobs
    } == {
        "a" * 64,
        "b" * 64,
    }


@pytest.mark.parametrize("launched,completed", [(False, False), (True, True), (True, False)])
def test_initial_profile_reseals_compatible_plan_only_after_terminal(
    tmp_path, monkeypatch, launched, completed
):
    binding = {"source_card": "1234abcd"}
    old_recipe = {"vitest": {"src/pages/Matter.test.tsx": 1}}
    new_recipe = {
        "vitest": {
            "src/pages/Matter.test.tsx": 1,
            "src/pages/Other.test.tsx": 1,
        }
    }
    previous_profile = {
        "qualified_by": "lumina-nor",
        "qualification_sha256": plan.sha(
            json.dumps(old_recipe, sort_keys=True, separators=(",", ":")).encode()
        ),
        "recipe": old_recipe,
        "node_environment": {"artifact_sha256": "a" * 64},
    }
    desired_profile = {
        **previous_profile,
        "qualified_by": "niobe",
        "qualification_sha256": plan.sha(
            json.dumps(new_recipe, sort_keys=True, separators=(",", ":")).encode()
        ),
        "recipe": new_recipe,
    }
    job = {
        "card": "1234abcd",
        "owner": "producer",
        "claim_revision": "claim-1",
        "workspace": str(tmp_path),
        "binding": binding,
        "execution_host": "chiap08",
        "initial_profile": True,
        "initial_profile_value": desired_profile,
        "plan_predecessor_sha256": "a" * 64,
    }
    card = SimpleNamespace(
        status=SimpleNamespace(value="doing"),
        owner="producer",
        meta={"_claim_revision": "claim-1"},
        model_dump=lambda **_kwargs: {},
    )
    current_plan = {
        "initial_profile_qualification": True,
        "profile": previous_profile,
        "host": "chiap08",
    }
    monkeypatch.setattr(
        refresh, "CardStore", lambda _home: SimpleNamespace(fold=lambda _card_id: card)
    )
    monkeypatch.setattr(plan, "source_state", lambda *_args: None)
    monkeypatch.setattr(
        plan,
        "load_plan",
        lambda *_args, **_kwargs: (current_plan, tmp_path / "plan.json", "a" * 64),
    )
    run = tmp_path / "run"
    if launched:
        run.mkdir()
        (run / "launch.json").write_text("{}")
    monkeypatch.setattr(plan, "run_directory", lambda *_args: run)
    monkeypatch.setattr(plan, "_completed_test_attempt", lambda *_args: completed)
    sealed = []
    monkeypatch.setattr(plan, "seal_plan", lambda *_args, **kwargs: sealed.append(kwargs))
    monkeypatch.setattr(tests, "run_or_read_tests", lambda *_args: None)

    if launched and not completed:
        with pytest.raises(
            plan.TestEvidenceError, match="qualification plan changed after launch"
        ):
            refresh._advance(tmp_path, {}, "/skcapstone", "niobe", None, job)
        assert sealed == []
        return
    assert refresh._advance(tmp_path, {}, "/skcapstone", "niobe", None, job) == "pending"
    expected = {
        "profile": desired_profile,
        "predecessor_sha256": "a" * 64,
        "execution_host": "chiap08",
        "initial_profile_qualification": True,
    }
    if launched:
        expected["acceptance_requalification"] = True
    assert sealed == [expected]


def test_initial_profile_expands_vitest_to_the_fixed_full_suite(tmp_path):
    web = tmp_path / "apps/web"
    (web / "src/pages").mkdir(parents=True)
    (web / "src/pages/Matter.test.tsx").write_text("test\n")
    (web / "src/pages/Other.test.ts").write_text("test\n")
    (web / "src/__snapshots__").mkdir()
    (web / "src/__snapshots__/Other.test.tsx.snap").write_text("snapshot\n")

    recipe = refresh._all_vitest_targets(
        {"pytest_all": True, "vitest": {"src/pages/Matter.test.tsx": 7}}, tmp_path
    )
    assert recipe == {
        "pytest_all": True,
        "vitest": {
            "src/pages/Matter.test.tsx": 7,
            "src/pages/Other.test.ts": 1,
        },
    }
    from skcapstone.fleet import production_test_node as node

    suites = ElementTree.Element(
        "testsuites",
        tests=str(sum(recipe["vitest"].values())),
        failures="0",
        errors="0",
        skipped="0",
    )
    for path, minimum in recipe["vitest"].items():
        suite = ElementTree.SubElement(
            suites,
            "testsuite",
            name=path,
            tests=str(minimum),
            failures="0",
            errors="0",
            skipped="0",
        )
        for index in range(minimum):
            ElementTree.SubElement(suite, "testcase", classname=path, name=f"passes-{index}")
    assert (
        node.junit_counts(ElementTree.tostring(suites), {"recipe": recipe})["per_file"]
        == recipe["vitest"]
    )


def test_existing_failure_replay_keeps_the_first_reason(tmp_path):
    path = tmp_path / ("a" * 64 + ".job.json")
    plan.private_dir(tmp_path, create=True)
    first = refresh._write_failure(path, "1234abcd", "first refusal")
    replay = refresh._write_failure(path, "1234abcd", "later refusal")
    assert first == replay == "first refusal"
    assert json.loads(path.with_name(path.stem + ".failed.json").read_text())["reason"] == first


def test_initial_profile_rejects_conflicting_profile_expansion():
    recipe = {"vitest": {"src/a.test.ts": 2}}
    previous = {
        "recipe": recipe,
        "qualification_sha256": plan.sha(
            json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
        ),
        "node_environment": {"a": 1},
    }
    expanded = {"vitest": {"src/a.test.ts": 2, "src/b.test.ts": 1}}
    assert refresh._compatible_initial_profile(
        previous,
        {
            "recipe": expanded,
            "qualification_sha256": plan.sha(
                json.dumps(expanded, sort_keys=True, separators=(",", ":")).encode()
            ),
            "node_environment": {"a": 1},
        },
    )
    assert not refresh._compatible_initial_profile(
        previous,
        {"recipe": {"vitest": {"src/a.test.ts": 1}}, "node_environment": {"a": 1}},
    )
    assert not refresh._compatible_initial_profile(
        previous,
        {"recipe": {"vitest": {"src/a.test.ts": 2}}, "node_environment": {"a": 2}},
    )


def test_initial_profile_does_not_retarget_a_launched_plan(tmp_path, monkeypatch):
    binding = {"source_card": "1234abcd"}
    value = {
        "qualified_by": "lumina-nor",
        "qualification_sha256": "e" * 64,
        "recipe": {"pytest_all": True},
    }
    job = {
        "card": "1234abcd",
        "owner": "producer",
        "claim_revision": "claim-1",
        "workspace": str(tmp_path),
        "binding": binding,
        "execution_host": "chiap08",
        "initial_profile": True,
        "initial_profile_value": value,
    }
    card = SimpleNamespace(
        status=SimpleNamespace(value="doing"),
        owner="producer",
        meta={"_claim_revision": "claim-1"},
    )
    run = tmp_path / "run"
    run.mkdir()
    (run / "launch.json").write_text("{}")
    monkeypatch.setattr(
        refresh, "CardStore", lambda _home: SimpleNamespace(fold=lambda _card_id: card)
    )
    monkeypatch.setattr(plan, "source_state", lambda *_args: None)
    monkeypatch.setattr(
        plan,
        "load_plan",
        lambda *_args, **_kwargs: (
            {"initial_profile_qualification": True, "profile": value, "host": "chiap03"},
            tmp_path / "plan.json",
            "a" * 64,
        ),
    )
    monkeypatch.setattr(plan, "run_directory", lambda *_args: run)

    with pytest.raises(
        plan.TestEvidenceError, match="qualification plan host changed after launch"
    ):
        refresh._advance(
            tmp_path, {"authority_host": "chiap08"}, "/skcapstone", "jarvis", None, job
        )


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


def test_harvest_skips_a_job_whose_workspace_git_fails(tmp_path, monkeypatch):
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

    def source_state(workspace, _binding):
        # A deleted workspace: git rev-parse HEAD exits 128 (8d4e7c5f, 2026-10-11).
        if workspace.name == "workspace-" + cards[0]:
            raise subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"])

    monkeypatch.setattr(refresh.plan, "source_state", source_state)
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

    assert state == "qualified:" + ",".join(cards[1:4])
    assert published == cards[1:4]


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
            card.status.value, card.owner = "doing", argv[argv.index("--agent") + 1]
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


def test_offer_binds_folded_labels_into_the_initial_recipe_core(tmp_path, monkeypatch):
    """Authority and builder must derive the recipe from the same labels."""
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
    seen = []
    monkeypatch.setattr(
        profile,
        "initial_recipe",
        lambda core, _ws: seen.append(core) or ({"pytest_all": True}, None),
    )
    monkeypatch.setattr(refresh.plan, "workspace_source_fingerprint", lambda *_args: "s" * 64)
    monkeypatch.setattr(refresh, "_execution_host", lambda *_args: "chiap08")
    card = SimpleNamespace(
        status=SimpleNamespace(value="ready"),
        owner=None,
        meta={},
        labels=["backend", "sklegal"],
        links={"test_profile_recipe": '{"pytest": {"tests/test_a.py": 1}}'},
    )
    monkeypatch.setattr(refresh.CardStore, "fold", lambda *_args: card)
    refresh.offer_stale_candidate(
        tmp_path,
        {"authority_host": "chiap08"},
        "/test/skcapstone",
        "niobe",
        {"id": "1234abcd"},
        ["sklegal"],
        lambda _core, _labels: tmp_path / "exact-source",
    )
    assert seen and seen[0]["labels"] == ["backend", "sklegal"]
    # The operator recipe link lives on the folded card, not the pool core.
    assert seen[0]["links"]["test_profile_recipe"] == '{"pytest": {"tests/test_a.py": 1}}'


def test_recent_failure_on_the_same_source_defers_the_offer(tmp_path):
    root = tmp_path / "fleet/profile-requalifications"
    root.mkdir(parents=True, mode=0o700)
    job = {"card": "1234abcd", "source_sha256": "s" * 64}
    plan.write_once(root / ("a" * 64 + ".job.json"), job)
    plan.write_once(
        root / ("a" * 64 + ".job.failed.json"),
        {"card": "1234abcd", "reason": "native unit, source or receipt custody mismatch"},
    )
    assert refresh._recent_failure(root, "1234abcd", "s" * 64)
    assert not refresh._recent_failure(root, "1234abcd", "t" * 64)
    assert not refresh._recent_failure(root, "9999aaaa", "s" * 64)


def test_failure_under_an_older_qualification_environment_is_a_new_attempt(tmp_path, monkeypatch):
    """Rebuilding qualify-env (new test dependencies) must not wait out the backoff."""
    import os

    from skcapstone.fleet.qualified_runtime import STATE_NAME

    root = tmp_path / "fleet/profile-requalifications"
    root.mkdir(parents=True, mode=0o700)
    plan.write_once(
        root / ("c" * 64 + ".job.json"), {"card": "1234abcd", "source_sha256": "s" * 64}
    )
    failed = root / ("c" * 64 + ".job.failed.json")
    plan.write_once(failed, {"card": "1234abcd", "reason": "full pytest suite failed"})
    prefix = tmp_path / "qualify-env"
    prefix.mkdir()
    manifest = prefix / STATE_NAME
    manifest.write_text("{}")
    monkeypatch.setattr(plan, "PREFIX", prefix)
    os.utime(failed, (failed.stat().st_mtime - 60,) * 2)
    os.utime(manifest, (failed.stat().st_mtime + 30,) * 2)
    assert not refresh._recent_failure(root, "1234abcd", "s" * 64)
    os.utime(manifest, (failed.stat().st_mtime - 30,) * 2)
    assert refresh._recent_failure(root, "1234abcd", "s" * 64)


def test_transient_failures_do_not_back_off(tmp_path):
    """A deploy that stales an in-flight plan must not park the card for hours."""
    root = tmp_path / "fleet/profile-requalifications"
    root.mkdir(parents=True, mode=0o700)
    plan.write_once(
        root / ("b" * 64 + ".job.json"), {"card": "1234abcd", "source_sha256": "s" * 64}
    )
    plan.write_once(
        root / ("b" * 64 + ".job.failed.json"),
        {"card": "1234abcd", "reason": "operator test plan is invalid or stale"},
    )
    assert not refresh._recent_failure(root, "1234abcd", "s" * 64)


def test_backlog_cards_are_offered_and_return_to_backlog(tmp_path, monkeypatch):
    """POOL_V2 dispatches approved backlog cards; a failure must not promote them."""
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
    card = SimpleNamespace(status=SimpleNamespace(value="backlog"), owner=None, meta={})
    monkeypatch.setattr(refresh.CardStore, "fold", lambda *_args: card)
    calls = []

    def coord(argv, **_kwargs):
        calls.append(argv[1:5])
        if argv[2] == "claim":
            card.status.value, card.owner = "doing", argv[argv.index("--agent") + 1]
            card.meta["_claim_revision"] = "claim-1"
        elif argv[2] == "release-claim":
            card.status.value, card.owner = "backlog", None
        elif argv[2] == "move":
            card.status.value = argv[4]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", coord)
    monkeypatch.setattr(refresh, "requalify_or_advance", lambda *_a, **_k: "failed:tests")
    state = refresh.offer_stale_candidate(
        tmp_path,
        {"authority_host": "chiap08"},
        "/test/skcapstone",
        "niobe",
        {"id": "1234abcd"},
        ["source-only"],
        lambda _core, _labels: tmp_path / "exact-source",
    )
    assert state == "failed:tests"
    assert ["coord", "claim", "1234abcd", "--agent"] in calls
    assert card.status.value == "backlog" and card.owner is None
    assert not any(c[1] == "move" for c in calls)


def test_failed_remote_job_releases_its_leaked_claim_back_to_ready(tmp_path, monkeypatch):
    """A remote executor failure writes .failed.json and nothing else.

    Production 2026-10-09: a69ec3a8, 90abb007 and 2587020f failed on chiap04
    ("operator test recipe is invalid") and stayed DOING, owned by
    niobe-requal-<card>, so the legacy selector counted them as claimed and
    they never reached POOL_V2.
    """
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    jobs = {
        "a": ("a69ec3a8", "niobe-requal-a69ec3a8", "rev-a", True),
        "b": ("90abb007", "niobe-requal-90abb007", "rev-b", True),
        "c": ("2587020f", "niobe-requal-2587020f", "rev-c", False),
    }
    cards = {}
    for suffix, (card_id, owner, revision, failed) in jobs.items():
        path = root / (suffix * 64 + ".job.json")
        plan.write_once(path, {"card": card_id, "owner": owner, "claim_revision": revision})
        if failed:
            plan.write_once(
                path.with_name(path.stem + ".failed.json"),
                {"card": card_id, "reason": "operator test recipe is invalid"},
            )
        cards[card_id] = SimpleNamespace(
            status=SimpleNamespace(value="doing"), owner=owner, meta={"_claim_revision": revision}
        )
    # 90abb007 was reclaimed by someone else since; its newer claim is not ours.
    cards["90abb007"].owner = "pi-glm-builder-node-chiap01-90abb007"
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh.CardStore, "fold", lambda _store, cid: cards[cid])
    monkeypatch.setattr(refresh, "_claim_is_current", lambda *_args: False)
    calls = []

    def coord(argv, **_kwargs):
        calls.append(argv[1:4])
        card = cards[argv[3]]
        if argv[2] == "release-claim":
            assert argv[argv.index("--owner") + 1] == card.owner
            assert argv[argv.index("--expected-claim-revision") + 1] == "rev-a"
            card.status.value, card.owner = "backlog", None
        elif argv[2] == "move":
            card.status.value = argv[4]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", coord)

    state = refresh.requalify_or_advance(
        tmp_path, {"authority_host": "chiap08"}, "/test/skcapstone", "niobe"
    )

    assert state == "batch:a69ec3a8=released"
    assert calls == [
        ["coord", "release-claim", "a69ec3a8"],
        ["coord", "move", "a69ec3a8"],
    ]
    assert cards["a69ec3a8"].status.value == "ready" and cards["a69ec3a8"].owner is None
    assert cards["90abb007"].owner == "pi-glm-builder-node-chiap01-90abb007"
    assert cards["2587020f"].owner == "niobe-requal-2587020f"
    # Idempotent: the next cycle finds nothing left to release.
    calls.clear()
    assert (
        refresh.requalify_or_advance(
            tmp_path, {"authority_host": "chiap08"}, "/test/skcapstone", "niobe"
        )
        == "idle"
    )
    assert calls == []


def test_changed_operator_recipe_is_not_backed_off(tmp_path):
    root = tmp_path / "fleet/profile-requalifications"
    root.mkdir(parents=True, mode=0o700)
    old_recipe = {"pytest": {"tests/test_a.py": 1}, "compile": [], "lint": [], "changelog": False}
    plan.write_once(
        root / ("c" * 64 + ".job.json"),
        {
            "card": "1234abcd",
            "source_sha256": "s" * 64,
            "initial_profile_value": {"recipe": old_recipe},
        },
    )
    plan.write_once(
        root / ("c" * 64 + ".job.failed.json"),
        {"card": "1234abcd", "reason": "operator test recipe is invalid: test target missing"},
    )
    same = json.dumps(old_recipe)
    new = json.dumps({**old_recipe, "pytest": {"tests/test_b.py": 2}})
    assert refresh._recent_failure(root, "1234abcd", "s" * 64, same)
    assert not refresh._recent_failure(root, "1234abcd", "s" * 64, new)


def test_rotate_advance_survives_a_harvest_failure():
    rotate = Path(__file__).resolve().parents[2] / "scripts/fleet/skfleet-rotate.py"
    source = rotate.read_text(encoding="utf-8")
    harvest = source.index("_harvest_state = harvest_completed(")
    blocked = source.index("PROFILE_REQUALIFICATION_HARVEST_BLOCKED", harvest)
    advance = source.index("_requal_state = requalify_or_advance(", harvest)
    assert harvest < blocked < advance


@pytest.mark.parametrize("state", ["older", "current", "unreadable"])
def test_failure_backoff_is_scoped_to_validated_harness_runtime(tmp_path, monkeypatch, state):
    root = tmp_path / "fleet/profile-requalifications"
    root.mkdir(parents=True, mode=0o700)
    binding = {"source_card": "1234abcd"}
    plan.write_once(
        root / ("a" * 64 + ".job.json"),
        {"card": "1234abcd", "source_sha256": "s" * 64, "binding": binding},
    )
    plan.write_once(
        root / ("a" * 64 + ".job.failed.json"),
        {"card": "1234abcd", "reason": "native unit, source or receipt custody mismatch"},
    )
    monkeypatch.setattr(plan, "PREFIX", tmp_path / "no-runtime-manifest")
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "b" * 64)

    def load(home, actual, **kwargs):
        assert home == tmp_path and actual == binding
        assert kwargs == {
            "require_current": False,
            "allow_completed": True,
            "allow_remote_host": True,
        }
        if state == "unreadable":
            raise plan.TestEvidenceError("untrusted plan")
        return {"runtime_sha256": ("a" if state == "older" else "b") * 64}, None, None

    monkeypatch.setattr(plan, "load_plan", load)
    assert refresh._recent_failure(root, "1234abcd", "s" * 64) is (state != "older")


def test_finished_profile_claim_stays_held_for_review_and_acceptance(monkeypatch):
    job = {"card": "1234abcd", "owner": "producer", "claim_revision": "claim-1"}
    card = SimpleNamespace(owner="producer", meta={"_claim_revision": "claim-1"})
    seen = []
    monkeypatch.setattr(
        "skcapstone.release_guard.finished_work_verdict",
        lambda *args: seen.append(args) or "PASS_FOR_REVIEW",
    )

    assert refresh._retained_finished_claim(Path("/unused"), job, card) == "PASS_FOR_REVIEW"
    assert seen == [(Path("/unused"), "1234abcd", "producer", "claim-1")]


def test_finished_profile_claim_refuses_changed_claim(monkeypatch):
    job = {"card": "1234abcd", "owner": "producer", "claim_revision": "claim-1"}
    card = SimpleNamespace(owner="producer", meta={"_claim_revision": "new-claim"})
    monkeypatch.setattr(
        "skcapstone.release_guard.finished_work_verdict", lambda *_args: "PASS_FOR_REVIEW"
    )

    with pytest.raises(plan.TestEvidenceError, match="finished source claim changed"):
        refresh._retained_finished_claim(Path("/unused"), job, card)


def test_existing_initial_profile_is_replayed_only_when_exact(tmp_path, monkeypatch):
    core = {"id": "1234abcd"}
    expected = {"card": "1234abcd", "source_sha256": "s" * 64}
    destination = tmp_path / "fleet/test-profiles/1234abcd.json"
    monkeypatch.setattr(
        profile,
        "qualify_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileExistsError()),
    )
    monkeypatch.setattr(profile, "read_profile", lambda *_args: (expected, "p" * 64))
    monkeypatch.setattr(profile, "profile_value", lambda *_args, **_kwargs: expected)

    assert (
        refresh._qualify_profile_or_replay(tmp_path, core, {}, {}, "lumina-nor", "r" * 64)
        == destination
    )


def test_existing_initial_profile_conflict_is_refused(tmp_path, monkeypatch):
    core = {"id": "1234abcd"}
    monkeypatch.setattr(
        profile,
        "qualify_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileExistsError()),
    )
    monkeypatch.setattr(profile, "read_profile", lambda *_args: ({"card": "different"}, "p" * 64))
    monkeypatch.setattr(
        profile,
        "profile_value",
        lambda *_args, **_kwargs: {"card": "1234abcd"},
    )

    with pytest.raises(plan.TestEvidenceError, match="qualified profile changed"):
        refresh._qualify_profile_or_replay(tmp_path, core, {}, {}, "lumina-nor", "r" * 64)
