"""Test that resolving a problem completes its associated GTD project.

Regression test for the lifecycle leak where create_problem discarded the
GTD project id and update_problem never completed it on resolve.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    """Redirect _shared_root() so GTD files land in tmp_path, not ~/.skcapstone."""
    import skcapstone.mcp_tools._helpers as _helpers

    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))


def test_resolving_problem_completes_its_gtd_project(tmp_path: Path):
    from skcapstone.itil import ITILManager
    from skcapstone.mcp_tools.gtd_tools import _load_list, _load_archive

    mgr = ITILManager(str(tmp_path))

    prb = mgr.create_problem(title="Flaky widget", managed_by="opus")
    assert prb.gtd_item_ids, "problem should store its GTD project id"
    assert any(p["id"] in prb.gtd_item_ids for p in _load_list("projects"))

    mgr.update_problem(prb.id, agent="opus", new_status="analyzing")
    mgr.update_problem(prb.id, agent="opus", new_status="resolved")

    assert not any(p["id"] in prb.gtd_item_ids for p in _load_list("projects"))
    archived = _load_archive()
    assert any(a["id"] in prb.gtd_item_ids and a["status"] == "done" for a in archived)
