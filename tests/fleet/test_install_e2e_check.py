"""End-to-end proof for `skfleet install --check` (task 10, epic
2026-08-16-skfleet-install).

Every other installer test stubs at least one seam in the load/diff/plan
chain (store.read_spec, nodeinventory.collect, profile_doctor.diff). This
test does not: it writes a real Profile spec into a real temp fleet store
via store.write_spec (the same path `skfleet apply` uses), stubs only the
node-facing edge (nodeinventory.collect, since no real systemd/package
metadata exists in CI), and drives run_install(mode="check") through the
whole chain -- store -> profile_doctor -> the run_install summary. If any
of those three modules disagree about shape, this is where it surfaces.
"""

from __future__ import annotations

from skcapstone.fleet import store
from skcapstone.fleet.installer import run_install


def test_run_install_check_reports_missing_required_units_end_to_end(
    paths, operator, monkeypatch
) -> None:
    # 1+2: a real Profile spec, written through the real store path, with a
    # required unit the (stubbed) node does not have installed.
    store.write_spec(
        paths,
        "profile",
        "control",
        {
            "units": {"required": ["sknoded.service"]},
            "packages": {"required": []},
        },
        writer=operator,
    )

    # 3: the node reports nothing installed -- required units show missing.
    monkeypatch.setattr(
        "skcapstone.fleet.installer.nodeinventory.collect",
        lambda: {"units": {"user": {}}, "packages": {}},
    )

    # 4: drive the real chain in check mode (report-only, never gated).
    summary = run_install(
        paths,
        "control",
        node="node-e2e",
        mode="check",
        dry_run=False,
        enable=False,
        start=False,
        only=None,
        backends={},
    )

    # 5: the required unit surfaces as a missing_required_units finding, and
    # the overall summary is not ok.
    assert summary["role"] == "control"
    assert summary["mode"] == "check"
    assert summary["ok"] is False
    missing = [r["name"] for r in summary["results"] if r["category"] == "missing_required_units"]
    assert "sknoded.service" in missing
