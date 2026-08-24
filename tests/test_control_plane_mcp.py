from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from mcp import types

from skdashboard.control_plane_client import (
    ClientResponse,
    ControlPlaneClient,
    ControlPlaneClientError,
)
from skdashboard.control_plane_fixture import BEARER, ORIGIN, REPORT, create_fixture_app
from skdashboard.control_plane_mcp import (
    ControlPlaneResources,
    _read_bearer,
    create_mcp_server,
)


async def _client() -> ControlPlaneClient:
    return await ControlPlaneClient.discover(
        ORIGIN + "/.well-known/skworld-module.json",
        BEARER,
        transport=httpx.ASGITransport(app=create_fixture_app()),
    )


def test_mcp_exposes_only_fixed_read_resources_and_one_report_template() -> None:
    async def run() -> None:
        client = await _client()
        try:
            resources = ControlPlaneResources(client)
            listed = resources.list()
            assert {str(item.uri) for item in listed} == {
                "skdashboard://control-plane/health",
                "skdashboard://control-plane/overview",
                "skdashboard://control-plane/board",
                "skdashboard://control-plane/fleet",
                "skdashboard://control-plane/economy",
            }
            assert all(item.mimeType == "application/json" for item in listed)
            assert [item.uriTemplate for item in resources.templates()] == [
                "skdashboard://control-plane/reports/{snapshot_id}"
            ]

            overview = await resources.read("skdashboard://control-plane/overview")
            report = await resources.read(
                f"skdashboard://control-plane/reports/{REPORT['snapshot_id']}"
            )
            assert overview.data["metrics"][0]["metric_id"] == "portfolio.synthetic_count"
            assert report.data == REPORT
            exposed = json.dumps([item.model_dump(mode="json") for item in listed])
            exposed += json.dumps(overview.data)
            assert BEARER not in exposed
            assert "matter_id" not in exposed
            assert "tenant_id" not in exposed
        finally:
            await client.aclose()

    asyncio.run(run())


def test_mcp_rejects_arbitrary_endpoints_queries_and_protected_results() -> None:
    class ProtectedClient:
        async def overview(self):
            return ClientResponse({"tenant_id": "must-not-leave"}, None)

    async def run() -> None:
        client = await _client()
        try:
            resources = ControlPlaneResources(client)
            for uri in (
                "https://example.test/api/v1/overview",
                "skdashboard://control-plane/actions/preview",
                "skdashboard://control-plane/reports/../../owner-state",
                f"skdashboard://control-plane/reports/{REPORT['snapshot_id']}?token=x",
            ):
                with pytest.raises(ControlPlaneClientError):
                    await resources.read(uri)
            with pytest.raises(ControlPlaneClientError, match="protected fields"):
                await ControlPlaneResources(ProtectedClient()).read(
                    "skdashboard://control-plane/overview"
                )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_mcp_server_registers_resources_without_any_tool_or_command_handler() -> None:
    async def run() -> None:
        client = await _client()
        try:
            server = create_mcp_server(client)
            assert types.ListResourcesRequest in server.request_handlers
            assert types.ListResourceTemplatesRequest in server.request_handlers
            assert types.ReadResourceRequest in server.request_handlers
            assert types.ListToolsRequest not in server.request_handlers
            assert types.CallToolRequest not in server.request_handlers
        finally:
            await client.aclose()

    asyncio.run(run())


def test_mcp_bearer_file_is_bounded_regular_and_mode_0600(tmp_path: Path) -> None:
    bearer = tmp_path / "read.cap"
    bearer.write_text("fixture-read\n", encoding="utf-8")
    bearer.chmod(0o600)
    assert _read_bearer(bearer) == "fixture-read"

    bearer.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        _read_bearer(bearer)
    bearer.chmod(0o600)

    link = tmp_path / "link.cap"
    link.symlink_to(bearer)
    with pytest.raises(ValueError, match="unsafe"):
        _read_bearer(link)

    bearer.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="read bound"):
        _read_bearer(bearer)

    bearer.write_bytes(b"first\nsecond")
    with pytest.raises(ValueError, match="content"):
        _read_bearer(bearer)
    os.chmod(bearer, 0o600)
