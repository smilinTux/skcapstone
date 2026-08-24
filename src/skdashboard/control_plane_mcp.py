"""MCP resources over the allowlisted read-only control-plane client."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents

from .control_plane_client import ClientResponse, ControlPlaneClient, ControlPlaneClientError

_FIXED = {
    "skdashboard://control-plane/health": ("Control-plane health", "health"),
    "skdashboard://control-plane/overview": ("Estate overview", "overview"),
    "skdashboard://control-plane/board": ("Board summary", "board"),
    "skdashboard://control-plane/fleet": ("Fleet summary", "fleet"),
    "skdashboard://control-plane/economy": ("Economy summary", "economy"),
}
_PROTECTED_KEYS = frozenset(
    {
        "tenant_id",
        "matter_id",
        "raw_content",
        "content_bytes",
        "capability_token",
        "bearer_token",
        "secret",
        "credential",
    }
)
_MAX_BEARER_BYTES = 64 * 1024


def _reject_protected(value: object) -> None:
    if isinstance(value, dict):
        if _PROTECTED_KEYS.intersection(value):
            raise ControlPlaneClientError("MCP resource contains protected fields")
        for child in value.values():
            _reject_protected(child)
    elif isinstance(value, list):
        for child in value:
            _reject_protected(child)


def _read_bearer(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("bearer file is unavailable or unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("bearer file must be one regular mode 0600 file")
        content = b""
        while len(content) <= _MAX_BEARER_BYTES:
            chunk = os.read(descriptor, 8192)
            if not chunk:
                break
            content += chunk
        if len(content) > _MAX_BEARER_BYTES:
            raise ValueError("bearer file exceeds its read bound")
    finally:
        os.close(descriptor)
    try:
        bearer = content.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("bearer file is not UTF-8") from error
    if not bearer or "\x00" in bearer or "\n" in bearer or "\r" in bearer:
        raise ValueError("bearer file content is invalid")
    return bearer


class ControlPlaneResources:
    """Expose fixed projections and hash-addressed reports, never tools."""

    def __init__(self, client: ControlPlaneClient):
        self.client = client

    def list(self) -> list[types.Resource]:
        return [
            types.Resource(
                name=name,
                uri=uri,
                description="Authorized, schema-validated read-only projection",
                mimeType="application/json",
            )
            for uri, (name, _operation) in _FIXED.items()
        ]

    def templates(self) -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                name="Immutable report snapshot",
                uriTemplate="skdashboard://control-plane/reports/{snapshot_id}",
                description="Authorized exact immutable report snapshot",
                mimeType="application/json",
            )
        ]

    async def read(self, uri: str) -> ClientResponse:
        if uri in _FIXED:
            response = await getattr(self.client, _FIXED[uri][1])()
            _reject_protected(response.data)
            return response
        parsed = urlsplit(uri)
        prefix = "/reports/"
        if (
            parsed.scheme == "skdashboard"
            and parsed.netloc == "control-plane"
            and parsed.path.startswith(prefix)
            and not parsed.query
            and not parsed.fragment
        ):
            response = await self.client.report(parsed.path[len(prefix) :])
            _reject_protected(response.data)
            return response
        raise ControlPlaneClientError("MCP resource URI is not allowlisted")


def create_mcp_server(client: ControlPlaneClient) -> Server:
    resources = ControlPlaneResources(client)
    server = Server("skdashboard-read-only")

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return resources.list()

    @server.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
        return resources.templates()

    @server.read_resource()
    async def read_resource(uri):
        response = await resources.read(str(uri))
        return [
            ReadResourceContents(
                content=json.dumps(response.data, sort_keys=True, separators=(",", ":")),
                mime_type="application/json",
                meta={"etag": response.etag} if response.etag else None,
            )
        ]

    return server


async def _run(discovery_url: str, bearer_file: Path) -> None:
    from mcp.server.stdio import stdio_server

    bearer = _read_bearer(bearer_file)
    client = await ControlPlaneClient.discover(discovery_url, bearer)
    server = create_mcp_server(client)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve read-only SKDashboard MCP resources")
    parser.add_argument("--discovery-url", required=True)
    parser.add_argument("--bearer-file", required=True, type=Path)
    args = parser.parse_args(argv)
    asyncio.run(_run(args.discovery_url, args.bearer_file))


__all__ = ["ControlPlaneResources", "create_mcp_server", "main"]
