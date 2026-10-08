"""The automatic refresh has one durable native qualification slot."""

import ast
from pathlib import Path

from skcapstone.fleet import production_test_plan as plan
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
