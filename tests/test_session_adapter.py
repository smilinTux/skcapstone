import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from skdashboard.read_only import create_read_only_app
from skdashboard.session_adapter import COOKIE_NAME, EncryptedSessionAdapter, SessionConfig

ORIGIN = "https://10.0.0.139:7778"


class Tokens:
    def __init__(self):
        self.calls = []

    async def exchange(self, values, *, expected_nonce=None):
        self.calls.append((values, expected_nonce))
        suffix = str(len(self.calls))
        return {
            "access_token": "access-" + suffix,
            "refresh_token": "refresh-" + suffix,
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "skdashboard.read skdashboard.events.read",
        }

    async def revoke(self, refresh_token):
        self.revoked = refresh_token


def adapter(tmp_path: Path, clock=lambda: 1_000, tokens=None):
    key = tmp_path / "session.key"
    key.write_bytes(Fernet.generate_key())
    os.chmod(key, 0o600)
    return EncryptedSessionAdapter(
        tmp_path / "state" / "sessions.db",
        key,
        SessionConfig(
            issuer="https://capauth.example",
            redirect_uri=f"{ORIGIN}/auth/callback",
            client_secret="test-secret",
        ),
        oidc_client=tokens or Tokens(),
        clock=clock,
    )


def login(client):
    start = client.get("/auth/login", follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    return query, client.get(
        "/auth/callback",
        params={"state": query["state"][0], "code": "one-use-code"},
        follow_redirects=False,
    )


def test_session_routes_are_opt_in_and_cookie_is_opaque(tmp_path):
    disabled = TestClient(create_read_only_app(tmp_path), base_url=ORIGIN)
    assert disabled.get("/auth/login").status_code == 404
    assert "Short-lived CapAuth bearer" in disabled.get("/").text
    session = adapter(tmp_path)
    client = TestClient(create_read_only_app(tmp_path, session_adapter=session), base_url=ORIGIN)
    assert "Sign in" in client.get("/").text
    assert "Short-lived CapAuth bearer" not in client.get("/").text
    query, response = login(client)
    assert response.status_code == 303
    assert query["code_challenge_method"] == ["S256"]
    assert query["issuer"] == ["https://capauth.example"]
    assert session.oidc.calls[0][1] == query["nonce"][0]
    cookie = response.headers["set-cookie"]
    assert COOKIE_NAME in cookie and "Secure" in cookie and "HttpOnly" in cookie
    assert "SameSite=strict" in cookie and "Domain=" not in cookie
    assert "access-" not in cookie and "refresh-" not in cookie
    data = (tmp_path / "state" / "sessions.db").read_bytes()
    assert b"access-1" not in data and b"refresh-1" not in data and b"test-secret" not in data


def test_state_is_one_use_and_restart_recovers_session(tmp_path):
    tokens = Tokens()
    first = adapter(tmp_path, tokens=tokens)
    client = TestClient(create_read_only_app(tmp_path, session_adapter=first), base_url=ORIGIN)
    query, response = login(client)
    assert response.status_code == 303
    assert (
        client.get(
            "/auth/callback", params={"state": query["state"][0], "code": "again"}
        ).status_code
        == 400
    )
    second = EncryptedSessionAdapter(
        first.database,
        tmp_path / "session.key",
        first.config,
        oidc_client=tokens,
        clock=lambda: 1_001,
    )
    calls = []
    app = create_read_only_app(
        tmp_path,
        session_adapter=second,
        authorizer=lambda bearer, capability, target: (
            calls.append((bearer, capability, target)) or True
        ),
    )
    restarted = TestClient(app, base_url=ORIGIN)
    restarted.cookies.update(client.cookies)
    assert restarted.get("/api/v1/overview", headers={"Origin": ORIGIN}).status_code == 200
    assert calls == [("access-1", "skdashboard.read", "/api/v1/overview")]


def test_refresh_rotates_server_credentials_and_pep_runs_each_request(tmp_path):
    now = [1_000]
    tokens = Tokens()
    session = adapter(tmp_path, clock=lambda: now[0], tokens=tokens)
    calls = []
    client = TestClient(
        create_read_only_app(
            tmp_path,
            session_adapter=session,
            authorizer=lambda bearer, *_: calls.append(bearer) or True,
        ),
        base_url=ORIGIN,
    )
    login(client)
    assert client.get("/api/v1/overview", headers={"Origin": ORIGIN}).status_code == 200
    now[0] = 1_290
    assert client.get("/api/v1/overview", headers={"Origin": ORIGIN}).status_code == 200
    assert calls == ["access-1", "access-2"]
    assert tokens.calls[-1] == (
        {"grant_type": "refresh_token", "refresh_token": "refresh-1"},
        None,
    )


def test_csrf_logout_and_corrupt_store_fail_closed(tmp_path):
    session = adapter(tmp_path)
    client = TestClient(create_read_only_app(tmp_path, session_adapter=session), base_url=ORIGIN)
    login(client)
    info = client.get("/auth/session")
    assert info.status_code == 200
    csrf = info.json()["csrf_token"]
    assert (
        client.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": "bad"}).status_code
        == 403
    )
    assert (
        client.post(
            "/auth/logout", headers={"Origin": "https://public.example", "X-CSRF-Token": csrf}
        ).status_code
        == 403
    )
    assert (
        client.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code
        == 204
    )
    assert client.get("/auth/session").status_code == 401


def test_key_permissions_and_token_response_are_strict(tmp_path):
    key = tmp_path / "bad.key"
    key.write_bytes(Fernet.generate_key())
    os.chmod(key, 0o644)
    try:
        EncryptedSessionAdapter(
            tmp_path / "db", key, SessionConfig("https://i", f"{ORIGIN}/auth/callback", "s")
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("permissive key was accepted")


def test_consistent_encrypted_backup_recovers_after_restart(tmp_path):
    first = adapter(tmp_path)
    client = TestClient(create_read_only_app(tmp_path, session_adapter=first), base_url=ORIGIN)
    login(client)
    backup = tmp_path / "backup" / "sessions.db"
    first.backup(backup)
    assert backup.stat().st_mode & 0o077 == 0
    assert b"access-1" not in backup.read_bytes()
    recovered = EncryptedSessionAdapter(
        backup,
        tmp_path / "session.key",
        first.config,
        oidc_client=Tokens(),
        clock=lambda: 1_001,
    )
    restored = TestClient(
        create_read_only_app(tmp_path, session_adapter=recovered, authorizer=lambda *_: True),
        base_url=ORIGIN,
    )
    restored.cookies.update(client.cookies)
    assert restored.get("/api/v1/overview", headers={"Origin": ORIGIN}).status_code == 200


def test_login_transactions_are_bounded_and_pruned(tmp_path):
    session = adapter(tmp_path)
    client = TestClient(create_read_only_app(tmp_path, session_adapter=session), base_url=ORIGIN)
    responses = [client.get("/auth/login", follow_redirects=False) for _ in range(17)]
    assert responses[-1].status_code == 429
    with session._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM login_transactions").fetchone()[0] == 16


def test_logout_requires_upstream_revocation(tmp_path):
    tokens = Tokens()
    session = adapter(tmp_path, tokens=tokens)
    client = TestClient(create_read_only_app(tmp_path, session_adapter=session), base_url=ORIGIN)
    login(client)
    csrf = client.get("/auth/session").json()["csrf_token"]
    assert client.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 204
    assert tokens.revoked == "refresh-1"


def test_only_bounded_session_post_route_is_added(tmp_path):
    app = create_read_only_app(tmp_path, session_adapter=adapter(tmp_path))
    posts = {route.path for route in app.routes if "POST" in (route.methods or set())}
    assert posts == {"/auth/logout"}
    for path in ("/api/card/x/mutate", "/api/assistant", "/api/cmdb/apply"):
        assert TestClient(app, base_url=ORIGIN).post(path).status_code == 404


def test_launcher_session_configuration_is_all_or_nothing(tmp_path, monkeypatch):
    from skdashboard.read_only import main

    key = tmp_path / "session.key"
    secret = tmp_path / "client.secret"
    key.write_bytes(Fernet.generate_key())
    secret.write_text("confidential", encoding="utf-8")
    os.chmod(key, 0o600)
    os.chmod(secret, 0o600)
    observed = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: observed.update(app=app, **kwargs))
    main(
        [
            "--home",
            str(tmp_path / "home"),
            "--host",
            "10.0.0.139",
            "--tls-certfile",
            "/run/cert",
            "--tls-keyfile",
            "/run/key",
            "--session-db",
            str(tmp_path / "state" / "sessions.db"),
            "--session-key-file",
            str(key),
            "--oidc-issuer",
            "https://capauth.example",
            "--oidc-redirect-uri",
            f"{ORIGIN}/auth/callback",
            "--oidc-client-secret-file",
            str(secret),
        ]
    )
    assert any(route.path == "/auth/login" for route in observed["app"].routes)
