"""Dedicated read-only SKDashboard control-plane runtime."""

from __future__ import annotations

import argparse
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .control_plane_api import ALLOWED_BROWSER_ORIGINS
from .control_plane_api import routes as control_plane_routes
from .dashboard import _get_agent_status, _get_board_state
from .skdashboard_manifest import skdashboard_module_manifest

ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "10.0.0.139", "100.81.238.58"})


def create_read_only_app(home: Path, *, authorizer=None) -> Starlette:
    """Build the least-privilege app without importing legacy route tables."""

    static_dir = Path(__file__).parent / "static"

    async def index(_request):
        return HTMLResponse((static_dir / "read_only.html").read_text(encoding="utf-8"))

    async def manifest(request):
        return JSONResponse(skdashboard_module_manifest(str(request.base_url)))

    routes = [Route("/", index), Route("/.well-known/skworld-module.json", manifest)]
    routes.extend(
        control_plane_routes(
            home,
            board_reader=_get_board_state,
            health_reader=_get_agent_status,
            authorizer=authorizer,
        )
    )
    routes.append(Mount("/static", StaticFiles(directory=str(static_dir))))
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
