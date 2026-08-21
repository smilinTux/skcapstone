from __future__ import annotations

from skcoord.cmdb import CMDBManager

from skdashboard.dashboard_cmdb import search


def test_search_matches_identity_metadata_tags_and_attributes(tmp_path):
    mgr = CMDBManager(tmp_path)
    mgr.create_ci(
        "Search API",
        "service",
        description="operator inventory endpoint",
        node="noroc2027",
        attributes={"port": 7778},
        tags=["dashboard"],
    )

    for query in ("search api", "SERVICE", "noroc2027", "dashboard", "7778"):
        result = search(tmp_path, query)
        assert result["total"] == 1
        assert result["items"][0]["name"] == "Search API"


def test_search_is_empty_for_blank_query_and_bounds_results(tmp_path):
    mgr = CMDBManager(tmp_path)
    for number in range(4):
        mgr.create_ci(f"worker-{number}", "service")

    assert search(tmp_path, "   ") == {"query": "", "total": 0, "items": []}
    result = search(tmp_path, "worker", limit=2)
    assert result["total"] == 4
    assert [item["name"] for item in result["items"]] == ["worker-0", "worker-1"]


def test_search_caps_requested_limit(tmp_path):
    mgr = CMDBManager(tmp_path)
    for number in range(105):
        mgr.create_ci(f"ci-{number:03d}", "service")

    assert len(search(tmp_path, "ci-", limit=1000)["items"]) == 100
    assert len(search(tmp_path, "ci-", limit="invalid")["items"]) == 50
