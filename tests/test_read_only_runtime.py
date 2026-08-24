from pathlib import Path

from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from skdashboard.read_only import ALLOWED_BIND_HOSTS, HSTS_POLICY, create_read_only_app, main

LAN_ORIGIN = "https://10.0.0.139:7778"
TAILNET_ORIGIN = "https://100.81.238.58:7778"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_read_only_app(tmp_path, authorizer=lambda *_: True), base_url=LAN_ORIGIN
    )


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
    headers = {"Authorization": "Bearer test", "Origin": LAN_ORIGIN}
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


def test_exact_https_origins_redirect_hsts_and_public_host_denial(tmp_path: Path) -> None:
    app = create_read_only_app(tmp_path, authorizer=lambda *_: True)
    for origin in (LAN_ORIGIN, TAILNET_ORIGIN):
        response = TestClient(app, base_url=origin).get("/api/v1/health")
        assert response.status_code == 200
        assert response.headers["strict-transport-security"] == HSTS_POLICY

    redirect = TestClient(app, base_url="http://10.0.0.139:7778").get(
        "/api/v1/health?probe=1", follow_redirects=False
    )
    assert redirect.status_code == 308
    assert redirect.headers["location"] == "https://10.0.0.139:7778/api/v1/health?probe=1"
    assert TestClient(app, base_url="https://public.example").get("/").status_code == 400


def test_cookie_transport_is_secure_host_only_and_session_routes_stay_disabled(
    tmp_path: Path,
) -> None:
    app = create_read_only_app(tmp_path, authorizer=lambda *_: True)

    async def synthetic_cookie(_request):
        return Response(headers={"Set-Cookie": "sid=test; Domain=example.test; HttpOnly"})

    app.routes.append(Route("/__transport_test__", synthetic_cookie))
    response = TestClient(app, base_url=LAN_ORIGIN).get("/__transport_test__")
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "Domain=" not in cookie
    assert not any("session" in route.path for route in app.routes)


def test_launcher_requires_exact_tls_files_without_persistent_transport_state(
    tmp_path: Path, monkeypatch
) -> None:
    observed = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: observed.update(kwargs))

    main(
        [
            "--home",
            str(tmp_path),
            "--host",
            "10.0.0.139",
            "--tls-certfile",
            "/run/credentials/skdashboard.crt",
            "--tls-keyfile",
            "/run/credentials/skdashboard.key",
        ]
    )

    assert observed["host"] == "10.0.0.139"
    assert observed["ssl_certfile"] == "/run/credentials/skdashboard.crt"
    assert observed["ssl_keyfile"] == "/run/credentials/skdashboard.key"
    assert list(tmp_path.iterdir()) == []


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
