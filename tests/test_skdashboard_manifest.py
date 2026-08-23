"""skdashboard's SKWorld module manifest: shape, origin-relative URLs, operator
facet, and the served /.well-known/skworld-module.json route."""

from skcapstone.skdashboard_manifest import (
    AUDIENCE,
    SCHEMA_VERSION,
    skdashboard_module_manifest,
)


def test_manifest_ui_facet_shape():
    m = skdashboard_module_manifest("http://127.0.0.1:7778/")
    assert m["schemaVersion"] == SCHEMA_VERSION
    assert m["id"] == "skdashboard"
    assert m["name"] == "Board"
    assert m["grade"] == "B"
    # nav.order 40 slots the Board after Code (30) and before the ops area.
    assert m["nav"] == {"icon": "dashboard", "order": 40, "label": "Board"}
    assert m["deeplinkPrefix"] == "skworld://skdashboard/"
    assert m["memory"] == {"opt_in": True, "scope": "skdashboard"}


def test_urls_are_origin_relative_and_not_double_slashed():
    m = skdashboard_module_manifest("http://host:7778/")
    assert m["entry"]["url"] == "http://host:7778/"
    assert m["health"] == "http://host:7778/api/status"
    # A base without a trailing slash yields the same (no missing/extra slash).
    m2 = skdashboard_module_manifest("http://host:7778")
    assert m2["entry"]["url"] == "http://host:7778/"
    assert m2["health"] == "http://host:7778/api/status"


def test_auth_facet_declares_audience_and_scopes():
    m = skdashboard_module_manifest("http://host/")
    assert m["auth"]["audience"] == AUDIENCE == "skdashboard"
    assert m["auth"]["scopes"] == ["skdashboard.read"]


def test_operator_facet_matches_the_skdashboard_adapter_contract():
    op = skdashboard_module_manifest("http://host/")["operator"]
    assert op["contractVersion"] == 1
    assert isinstance(op["contractVersion"], int)
    assert op["cli"] == "skcapstone dashboard operator"
    assert op["repos"] == ["skcapstone"]
    # Mirrors operator_seat/skdashboard_adapter.py CONDITIONS and its standard actions.
    assert op["conditions"] == ["DashboardReady", "BoardReadable"]
    assert op["proposedStandardActions"] == ["restart-dashboard"]


def test_well_known_route_serves_origin_relative_manifest(tmp_path):
    """The dashboard's web server serves the manifest at the well-known path with
    URLs origin-relative to the request (mirrors skchat's webui.py route)."""
    from starlette.testclient import TestClient

    from skcapstone.dashboard import create_app

    app = create_app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/.well-known/skworld-module.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "skdashboard"
    assert body["schemaVersion"] == SCHEMA_VERSION
    # Origin-relative: the served entry URL resolves against the request host.
    assert body["entry"]["url"] == "http://testserver/"
    assert body["health"] == "http://testserver/api/status"
    assert body["operator"]["conditions"] == ["DashboardReady", "BoardReadable"]
