"""Dedicated read-only SKDashboard control-plane runtime."""

from __future__ import annotations

import argparse
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .control_plane_api import ALLOWED_BROWSER_ORIGINS
from .control_plane_api import routes as control_plane_routes
from .dashboard import _get_agent_status, _get_board_state

ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "10.0.0.139", "100.81.238.58"})


def create_read_only_app(
    home: Path,
    *,
    authorizer=None,
    decision_authorizer=None,
    invocation_factory=None,
    project_provider=None,
    schedule_provider=None,
) -> Starlette:
    """Build the least-privilege app without importing legacy route tables."""

    static_dir = Path(__file__).parent / "static"

    async def index(_request):
        return HTMLResponse((static_dir / "read_only.html").read_text(encoding="utf-8"))

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
        )
    )
    return Starlette(routes=routes)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the read-only SKDashboard control plane")
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--host", required=True, choices=sorted(ALLOWED_BIND_HOSTS))
    parser.add_argument("--port", type=int, default=7778)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    import uvicorn

    uvicorn.run(create_read_only_app(args.home), host=args.host, port=args.port)


__all__ = ["ALLOWED_BIND_HOSTS", "ALLOWED_BROWSER_ORIGINS", "create_read_only_app", "main"]
