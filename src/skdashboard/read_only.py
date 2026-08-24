"""Dedicated read-only SKDashboard control-plane runtime."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .control_plane_api import ALLOWED_BROWSER_ORIGINS
from .control_plane_api import routes as control_plane_routes
from .dashboard import _get_agent_status, _get_board_state

ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "10.0.0.139", "100.81.238.58"})
HSTS_POLICY = "max-age=31536000"


class SecureTransportMiddleware:
    """Deny unnamed hosts and enforce HTTPS response transport."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope["headers"]}
        authority = headers.get(b"host", b"").decode("ascii", "ignore")
        host = authority.rsplit(":", 1)[0].lower()
        if host not in ALLOWED_BIND_HOSTS:
            await _plain_response(send, 400, b"named host required")
            return
        if scope["scheme"] != "https":
            path = quote(scope.get("path", "/"), safe="/%:@")
            query = scope.get("query_string", b"")
            location = f"https://{authority}{path}".encode()
            if query:
                location += b"?" + query
            await _plain_response(send, 308, b"HTTPS required", [(b"location", location)])
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                secured = [(b"strict-transport-security", HSTS_POLICY.encode())]
                for key, value in message.get("headers", []):
                    if key.lower() == b"set-cookie":
                        value = _secure_host_only_cookie(value)
                    secured.append((key, value))
                message["headers"] = secured
            await send(message)

        await self.app(scope, receive, secure_send)


async def _plain_response(send, status: int, body: bytes, headers=()):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), *headers],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _secure_host_only_cookie(value: bytes) -> bytes:
    parts = [part.strip() for part in value.decode("latin-1").split(";")]
    attributes = [part for part in parts[1:] if not part.lower().startswith("domain=")]
    if not any(part.lower() == "secure" for part in attributes):
        attributes.append("Secure")
    return "; ".join([parts[0], *attributes]).encode("latin-1")


def create_read_only_app(
    home: Path,
    *,
    authorizer=None,
    decision_authorizer=None,
    invocation_factory=None,
    project_provider=None,
    schedule_provider=None,
    reliability_provider=None,
    session_adapter=None,
    architecture_provider=None,
) -> Starlette:
    """Build the least-privilege app without importing legacy route tables."""

    static_dir = Path(__file__).parent / "static"
    if reliability_provider is None and decision_authorizer is not None:
        from .dashboard_itil import ReliabilityProjectionProvider

        reliability_provider = ReliabilityProjectionProvider()
    if architecture_provider is None and decision_authorizer is not None:
        from .dashboard_architecture import ArchitectureProjectionProvider

        architecture_provider = ArchitectureProjectionProvider()

    async def index(_request):
        name = "read_only_session.html" if session_adapter is not None else "read_only.html"
        return HTMLResponse((static_dir / name).read_text(encoding="utf-8"))

    async def manifest(request):
        base = str(request.base_url).rstrip("/")
        return JSONResponse(
            {
                "schemaVersion": "1.1",
                "id": "skdashboard-read-only",
                "name": "SK Control Plane",
                "grade": "B",
                "entry": {"url": f"{base}/"},
                "nav": {"icon": "dashboard", "order": 40, "label": "Control Plane"},
                "auth": {"audience": "skdashboard", "scopes": ["skdashboard.read"]},
                "health": f"{base}/api/v1/health",
            }
        )

    routes = [Route("/", index), Route("/.well-known/skworld-module.json", manifest)]
    routes.extend(
        control_plane_routes(
            home,
            board_reader=_get_board_state,
            health_reader=_get_agent_status,
            authorizer=authorizer,
            decision_authorizer=decision_authorizer,
            invocation_factory=invocation_factory,
            project_provider=project_provider,
            schedule_provider=schedule_provider,
            reliability_provider=reliability_provider,
            session_resolver=session_adapter.resolve if session_adapter else None,
            architecture_provider=architecture_provider,
        )
    )
    if session_adapter is not None:
        routes.extend(session_adapter.routes())
    app = Starlette(routes=routes)
    app.add_middleware(SecureTransportMiddleware)
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the read-only SKDashboard control plane")
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--host", required=True, choices=sorted(ALLOWED_BIND_HOSTS))
    parser.add_argument("--port", type=int, default=7778)
    parser.add_argument("--tls-certfile", type=Path, required=True)
    parser.add_argument("--tls-keyfile", type=Path, required=True)
    parser.add_argument("--session-db", type=Path)
    parser.add_argument("--session-key-file", type=Path)
    parser.add_argument("--oidc-issuer")
    parser.add_argument("--oidc-redirect-uri")
    parser.add_argument("--oidc-client-secret-file", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    session_values = (
        args.session_db,
        args.session_key_file,
        args.oidc_issuer,
        args.oidc_redirect_uri,
        args.oidc_client_secret_file,
    )
    if any(session_values) and not all(session_values):
        parser.error("all session and OIDC options are required together")
    session_adapter = None
    if all(session_values):
        from .session_adapter import EncryptedSessionAdapter, SessionConfig

        if args.oidc_client_secret_file.stat().st_mode & 0o077:
            parser.error("OIDC client secret file must be mode 0600")
        session_adapter = EncryptedSessionAdapter(
            args.session_db,
            args.session_key_file,
            SessionConfig(
                issuer=args.oidc_issuer,
                redirect_uri=args.oidc_redirect_uri,
                client_secret=args.oidc_client_secret_file.read_text(encoding="utf-8").strip(),
            ),
        )

    import uvicorn

    uvicorn.run(
        create_read_only_app(args.home, session_adapter=session_adapter),
        host=args.host,
        port=args.port,
        ssl_certfile=str(args.tls_certfile),
        ssl_keyfile=str(args.tls_keyfile),
    )


__all__ = [
    "ALLOWED_BIND_HOSTS",
    "ALLOWED_BROWSER_ORIGINS",
    "HSTS_POLICY",
    "SecureTransportMiddleware",
    "create_read_only_app",
    "main",
]
