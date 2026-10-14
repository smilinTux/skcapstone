"""Historical qualification failures cannot veto a new exact qualification."""

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import profile_requalification as refresh


@pytest.mark.parametrize("terminal", ["done", "failed"])
def test_terminal_job_is_not_revalidated(tmp_path, monkeypatch, terminal):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    job = root / ("a" * 64 + ".job.json")
    plan.write_once(
        job,
        {
            "schema": "skfleet.profile-requalification/v1",
            "card": "1234abcd",
            "binding": {},
            "workspace": "/old",
        },
    )
    plan.write_once(job.with_name(job.stem + "." + terminal + ".json"), {"terminal": True})
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")

    def reject(*a, **kw):
        raise plan.TestEvidenceError("operator test plan is invalid or stale")

    monkeypatch.setattr(refresh.plan, "load_plan", reject)
    assert refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}, "1234abcd") == "idle"


@pytest.mark.parametrize("obsolete", ["runtime", "predecessor", None])
def test_orphan_history_does_not_mask_current_invalid_plan(tmp_path, monkeypatch, obsolete):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    plans = tmp_path / "fleet/test-plans"
    plan.private_dir(plans, create=True)
    raw = {
        "binding": {"source_card": "1234abcd"},
        "profile_requalification": True,
        "runtime_sha256": ("a" if obsolete == "runtime" else "b") * 64,
        "profile_predecessor_sha256": ("c" if obsolete == "predecessor" else "d") * 64,
    }
    plan.write_once(plans / "old.json", raw)
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh.plan, "runtime_fingerprint", lambda: "b" * 64)
    monkeypatch.setattr(refresh.profile, "read_profile", lambda *a: ({}, "d" * 64))

    def reject(*a, **kw):
        raise plan.TestEvidenceError("operator test plan is invalid or stale")

    monkeypatch.setattr(refresh.plan, "load_plan", reject)
    result = refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}, "1234abcd")
    assert result == ("idle" if obsolete else "blocked:operator test plan is invalid or stale")


@pytest.mark.parametrize("terminal", ["done", "failed"])
def test_terminal_job_plan_cannot_reenter_through_orphan_scan(tmp_path, monkeypatch, terminal):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    plans = tmp_path / "fleet/test-plans"
    plan.private_dir(plans, create=True)
    binding = {"source_card": "1234abcd", "source_claim_revision": "old-claim"}
    job = root / ("a" * 64 + ".job.json")
    plan.write_once(
        job,
        {
            "schema": "skfleet.profile-requalification/v1",
            "card": "1234abcd",
            "binding": binding,
            "workspace": "/recorded-workspace",
        },
    )
    plan.write_once(job.with_name(job.stem + "." + terminal + ".json"), {"terminal": True})
    plan.write_once(
        plans / "old.json",
        {
            "binding": binding,
            "profile_requalification": True,
            "runtime_sha256": "b" * 64,
            "profile_predecessor_sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh.plan, "runtime_fingerprint", lambda: "b" * 64)
    monkeypatch.setattr(refresh.profile, "read_profile", lambda *a: ({}, "d" * 64))

    def reject(*a, **kw):
        raise plan.TestEvidenceError("terminal job rediscovered as orphan")

    monkeypatch.setattr(refresh.plan, "load_plan", reject)
    assert refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}, "1234abcd") == "idle"
