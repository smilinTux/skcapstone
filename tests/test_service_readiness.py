"""Negative controls for explicit application readiness, using a local HTTP fixture."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from skcapstone.service_readiness import check_http_readiness


@pytest.fixture
def endpoint():
    """Serve controllable responses without touching production."""
    state = {"status": 200, "body": b'{"maintenance":false}', "agent": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["agent"] = self.headers.get("User-Agent")
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "/login")
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/ready", state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize(
    "body", [b'{"maintenance":true}', b"{}", b"broken", b"[]", b'{"maintenance":0}']
)
def test_unready_or_unobserved_json_never_passes(endpoint, body):
    url, state = endpoint
    state["body"] = body
    result = check_http_readiness("app", url, expected_json={"maintenance": False})
    assert result["status"] == "down"
    assert result["error_class"] == "not_ready"


def test_healthy_json_and_truthful_user_agent(endpoint):
    url, state = endpoint
    result = check_http_readiness(
        "app", url, expected_json={"maintenance": False}, user_agent="SKCapstone-Readiness/1.0"
    )
    assert result["status"] == "up"
    assert state["agent"] == "SKCapstone-Readiness/1.0"


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_policy_or_unavailable_endpoint_is_unknown_not_backend_failure(endpoint, status):
    url, state = endpoint
    state["status"] = status
    result = check_http_readiness("app", url)
    assert result["status"] == "unknown"
    assert result["error_class"] == "http_policy_or_endpoint"
    assert result["http_status"] == status


def test_backend_503_is_down(endpoint):
    url, state = endpoint
    state["status"] = 503
    result = check_http_readiness("app", url)
    assert result["status"] == "down"
    assert result["error_class"] == "not_ready"


def test_redirect_never_certifies_login_page(endpoint):
    url, state = endpoint
    state["status"] = 302
    result = check_http_readiness("app", url)
    assert result["status"] == "unknown"
    assert result["error_class"] == "http_policy_or_endpoint"


def test_invalid_config_is_unknown(endpoint):
    result = check_http_readiness("app", endpoint[0], expected_json={"nested": {"x": 1}})
    assert result["status"] == "unknown"
    assert result["error_class"] == "invalid_config"


def test_body_limit_fails_closed(endpoint):
    url, state = endpoint
    state["body"] = b" " * 65537
    result = check_http_readiness("app", url, expected_json={"maintenance": False})
    assert result["status"] == "down"


def test_registry_dispatches_explicit_readiness(monkeypatch, endpoint):
    from skcapstone import service_health

    monkeypatch.setattr(
        service_health,
        "_load_registry_entries",
        lambda: [
            {
                "name": "asserted-app",
                "health_url": endpoint[0],
                "health_readiness": True,
                "health_expected_json": {"maintenance": False},
                "health_user_agent": "SKCapstone-Test/1",
            }
        ],
    )
    monkeypatch.setattr(
        service_health, "_http_check", lambda name, *a, **k: {"name": name, "status": "unknown"}
    )
    monkeypatch.setattr(
        service_health, "_tcp_check", lambda name, *a, **k: {"name": name, "status": "unknown"}
    )
    result = next(r for r in service_health.check_all_services() if r["name"] == "asserted-app")
    assert result["status"] == "up"
    assert endpoint[1]["agent"] == "SKCapstone-Test/1"


def test_sdk_preserves_legacy_and_explicit_options(tmp_path):
    from skcapstone.sdk import register_service

    legacy = json.loads(
        __import__("pathlib")
        .Path(register_service("legacy", "http://localhost/health", home=tmp_path))
        .read_text()
    )
    assert "health_readiness" not in legacy
    path = register_service(
        "ready",
        "https://example.test/ready",
        home=tmp_path,
        readiness=True,
        expected_json={"maintenance": False},
        user_agent="SKCapstone-Test/1",
    )
    entry = json.loads(__import__("pathlib").Path(path).read_text())
    assert entry["health_readiness"] is True
    assert entry["health_expected_json"] == {"maintenance": False}
    assert entry["health_user_agent"] == "SKCapstone-Test/1"


def test_sdk_rejects_readiness_options_without_opt_in(tmp_path):
    from skcapstone.sdk import register_service

    with pytest.raises(ValueError, match="readiness=True"):
        register_service("bad", "https://example.test/", home=tmp_path, expected_json={"ok": True})
    assert not (tmp_path / "registry").exists()


def test_malformed_registry_flag_cannot_fall_back_to_reachability(monkeypatch):
    from skcapstone import service_health

    monkeypatch.setattr(
        service_health,
        "_load_registry_entries",
        lambda: [
            {
                "name": "misconfigured",
                "health_url": "https://example.test",
                "health_readiness": "true",
            }
        ],
    )
    monkeypatch.setattr(
        service_health, "_http_check", lambda name, *a, **k: {"name": name, "status": "up"}
    )
    monkeypatch.setattr(
        service_health, "_tcp_check", lambda name, *a, **k: {"name": name, "status": "up"}
    )
    result = next(r for r in service_health.check_all_services() if r["name"] == "misconfigured")
    assert result["status"] == "unknown"
    assert result["error_class"] == "invalid_config"
