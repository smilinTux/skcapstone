from pathlib import Path

from starlette.testclient import TestClient

from skdashboard.read_only import ALLOWED_BIND_HOSTS, create_read_only_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_read_only_app(tmp_path, authorizer=lambda *_: True))


def test_route_inventory_is_read_only(tmp_path: Path) -> None:
    app = create_read_only_app(tmp_path, authorizer=lambda *_: True)
    routes = {
        (getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in app.routes
    }
    assert not any("POST" in methods for _, methods in routes)
    assert {"127.0.0.1", "10.0.0.139", "100.81.238.58"} == ALLOWED_BIND_HOSTS


def test_approved_surfaces_exist_and_legacy_privilege_is_absent(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/").status_code == 200
    assert client.get("/.well-known/skworld-module.json").status_code == 200
    manifest = client.get("/.well-known/skworld-module.json").json()
    assert manifest["health"].endswith("/api/v1/health")
    assert manifest["auth"] == {"audience": "skdashboard", "scopes": ["skdashboard.read"]}
    assert "operator" not in manifest
    assert client.get("/api/v1/health").status_code == 200
    headers = {"Authorization": "Bearer test", "Origin": "http://10.0.0.139:7778"}
    assert client.get("/api/v1/overview", headers=headers).status_code == 200
    assert client.get("/metrics", headers=headers).status_code == 200
    for path in (
        "/api/auth/capability",
        "/api/card/x/mutate",
        "/api/card/x/queue-ai",
        "/api/assistant",
        "/api/cmdb/apply",
        "/api/cmdb/seed",
        "/api/models/advertise",
        "/static/assistant.html",
        "/static/cmdb.html",
        "/static/models.html",
        "/static/js/api.js",
        "/static/js/assistant.js",
        "/static/js/cmdb.js",
        "/static/js/ai_compose.js",
    ):
        assert client.get(path).status_code == 404
        assert client.post(path).status_code == 404


def test_protected_routes_keep_auth_and_origin_denials(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/v1/overview").status_code == 401
    assert (
        client.get(
            "/api/v1/overview",
            headers={"Authorization": "Bearer test", "Origin": "https://public.example"},
        ).status_code
        == 403
    )
