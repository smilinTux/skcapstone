"""Read-only Tailscale endpoint reconciliation."""

from __future__ import annotations

import json

from click.testing import CliRunner

from skcapstone.fleet import endpoint_audit, store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.paths import FleetPaths


def _node(paths: FleetPaths) -> None:
    store.write_spec(
        paths,
        "node",
        "chiwk11",
        {
            "address": {"hostname": "chiwk11"},
            "addresses": [{"kind": "tailscale-windows", "value": "100.66.248.110"}],
            "aliases": ["chiwk11-1", "chiwk11-wsl"],
        },
        writer=store.Writer(role="operator", node="test", identity="test"),
    )


def _status(*, second_online: bool = False) -> dict:
    return {
        "Self": {"ID": "self", "HostName": "chiap08", "Online": True},
        "Peer": {
            "stale": {
                "ID": "old-id",
                "HostName": "chiwk11",
                "DNSName": "chiwk11.tail.test.",
                "TailscaleIPs": ["100.116.214.121"],
                "Online": second_online,
                "LastSeen": "2026-08-21T17:23:22Z",
                "OS": "windows",
            },
            "active": {
                "ID": "new-id",
                "HostName": "chiwk11",
                "DNSName": "chiwk11-1.tail.test.",
                "TailscaleIPs": ["100.66.248.110"],
                "Online": True,
                "OS": "windows",
                "Tags": ["tag:server"],
            },
        },
    }


def test_duplicate_with_one_declared_active_endpoint_is_safe_but_flagged(tmp_path) -> None:
    paths = FleetPaths(tmp_path)
    _node(paths)

    result = endpoint_audit.audit(paths, _status())
    report = result["reports"][0]

    assert result["read_only"] is True
    assert report["safe_to_route"] is True
    assert report["active_peer_id"] == "new-id"
    assert report["retirement_candidates"] == ["old-id"]
    assert {item["kind"] for item in report["findings"]} == {
        "duplicate_tailscale_identity",
        "stale_registration",
    }


def test_two_active_identities_fail_closed(tmp_path) -> None:
    paths = FleetPaths(tmp_path)
    _node(paths)

    report = endpoint_audit.audit(paths, _status(second_online=True))["reports"][0]

    assert report["safe_to_route"] is False
    assert report["active_peer_id"] is None
    assert any(item["kind"] == "ambiguous_active_endpoint" for item in report["findings"])


def test_declared_windows_and_wsl_runtime_routes_are_safe(tmp_path) -> None:
    paths = FleetPaths(tmp_path)
    store.write_spec(
        paths,
        "node",
        "chiwk12",
        {
            "address": {"hostname": "chiwk12"},
            "addresses": [
                {"kind": "tailscale-wsl", "value": "100.120.22.21"},
                {"kind": "tailscale-windows", "value": "100.87.143.116"},
            ],
            "aliases": ["chiwk12-windows", "chiwk12-wsl"],
        },
        writer=store.Writer(role="operator", node="test", identity="test"),
    )
    status = {
        "Peer": {
            "wsl": {
                "ID": "wsl-id",
                "HostName": "chiwk12",
                "TailscaleIPs": ["100.120.22.21"],
                "Online": True,
                "OS": "linux",
            },
            "windows": {
                "ID": "windows-id",
                "HostName": "chiwk12",
                "TailscaleIPs": ["100.87.143.116"],
                "Online": True,
                "OS": "windows",
            },
        }
    }

    report = endpoint_audit.audit(paths, status)["reports"][0]

    assert report["safe_to_route"] is True
    assert report["severity"] == "ok"
    assert report["active_peer_id"] is None
    assert report["active_routes"] == [
        {
            "endpoint": "100.87.143.116",
            "kind": "tailscale-windows",
            "os": "windows",
            "peer_id": "windows-id",
        },
        {
            "endpoint": "100.120.22.21",
            "kind": "tailscale-wsl",
            "os": "linux",
            "peer_id": "wsl-id",
        },
    ]
    assert {item["kind"] for item in report["findings"]} == {"declared_multi_runtime"}


def test_declared_multi_runtime_with_wrong_os_fails_closed(tmp_path) -> None:
    paths = FleetPaths(tmp_path)
    store.write_spec(
        paths,
        "node",
        "chiwk12",
        {
            "address": {"hostname": "chiwk12"},
            "addresses": [
                {"kind": "tailscale-wsl", "value": "100.120.22.21"},
                {"kind": "tailscale-windows", "value": "100.87.143.116"},
            ],
        },
        writer=store.Writer(role="operator", node="test", identity="test"),
    )
    status = {
        "Peer": {
            "one": {
                "ID": "one",
                "HostName": "chiwk12",
                "TailscaleIPs": ["100.120.22.21"],
                "Online": True,
                "OS": "windows",
            },
            "two": {
                "ID": "two",
                "HostName": "chiwk12",
                "TailscaleIPs": ["100.87.143.116"],
                "Online": True,
                "OS": "windows",
            },
        }
    }

    report = endpoint_audit.audit(paths, status)["reports"][0]

    assert report["safe_to_route"] is False
    assert any(item["kind"] == "ambiguous_active_endpoint" for item in report["findings"])


def test_wsl_only_policy_rejects_active_windows_peer(tmp_path) -> None:
    paths = FleetPaths(tmp_path)
    store.write_spec(
        paths,
        "node",
        "chiwk12",
        {
            "address": {"hostname": "chiwk12"},
            "addresses": [
                {"kind": "tailscale-wsl", "value": "100.120.22.21"},
                {"kind": "tailscale-windows", "value": "100.87.143.116"},
            ],
            "tailscale": {"allowed_os": ["linux"], "max_active_peers": 1},
        },
        writer=store.Writer(role="operator", node="test", identity="test"),
    )
    status = {
        "Peer": {
            "wsl": {
                "ID": "wsl-id",
                "HostName": "chiwk12",
                "TailscaleIPs": ["100.120.22.21"],
                "Online": True,
                "OS": "linux",
            },
            "windows": {
                "ID": "windows-id",
                "HostName": "chiwk12",
                "TailscaleIPs": ["100.87.143.116"],
                "Online": True,
                "OS": "windows",
            },
        }
    }

    report = endpoint_audit.audit(paths, status)["reports"][0]

    assert report["safe_to_route"] is False
    assert report["policy"] == {"allowed_os": ["linux"], "max_active_peers": 1}
    assert {item["kind"] for item in report["findings"]} >= {
        "disallowed_peer_os",
        "active_peer_limit_exceeded",
    }


def test_wsl_only_policy_accepts_one_declared_linux_peer(tmp_path) -> None:
    paths = FleetPaths(tmp_path)
    store.write_spec(
        paths,
        "node",
        "chiwk12",
        {
            "address": {"hostname": "chiwk12"},
            "addresses": [{"kind": "tailscale-wsl", "value": "100.120.22.21"}],
            "tailscale": {"allowed_os": ["linux"], "max_active_peers": 1},
        },
        writer=store.Writer(role="operator", node="test", identity="test"),
    )
    status = {
        "Peer": {
            "wsl": {
                "ID": "wsl-id",
                "HostName": "chiwk12",
                "TailscaleIPs": ["100.120.22.21"],
                "Online": True,
                "OS": "linux",
            }
        }
    }

    report = endpoint_audit.audit(paths, status)["reports"][0]

    assert report["safe_to_route"] is True
    assert report["active_peer_id"] == "wsl-id"
    assert report["findings"] == []


def test_endpoint_mismatch_never_selects_an_observed_peer(tmp_path) -> None:
    paths = FleetPaths(tmp_path)
    _node(paths)
    status = _status()
    status["Peer"]["active"]["TailscaleIPs"] = ["100.64.0.99"]

    report = endpoint_audit.audit(paths, status)["reports"][0]

    assert report["safe_to_route"] is False
    assert any(item["kind"] == "configured_endpoint_mismatch" for item in report["findings"])


def test_cli_reads_fixture_reports_candidates_and_writes_nothing(tmp_path, monkeypatch) -> None:
    paths = FleetPaths(tmp_path / "fleet")
    _node(paths)
    fixture = tmp_path / "status.json"
    fixture.write_text(json.dumps(_status()), encoding="utf-8")
    before = sorted(
        (path.relative_to(paths.root), path.read_bytes())
        for path in paths.root.rglob("*")
        if path.is_file()
    )
    monkeypatch.setenv("SKFLEET_ROOT", str(paths.root))

    result = CliRunner().invoke(
        fleet, ["node", "endpoint-audit", "--status-file", str(fixture), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"] == {
        "duplicates": 1,
        "nodes": 1,
        "retirement_candidates": 1,
        "unsafe": 0,
    }
    after = sorted(
        (path.relative_to(paths.root), path.read_bytes())
        for path in paths.root.rglob("*")
        if path.is_file()
    )
    assert after == before


def test_cli_strict_exits_one_for_ambiguous_active_endpoint(tmp_path, monkeypatch) -> None:
    paths = FleetPaths(tmp_path / "fleet")
    _node(paths)
    fixture = tmp_path / "status.json"
    fixture.write_text(json.dumps(_status(second_online=True)), encoding="utf-8")
    monkeypatch.setenv("SKFLEET_ROOT", str(paths.root))

    result = CliRunner().invoke(
        fleet, ["node", "endpoint-audit", "--status-file", str(fixture), "--strict"]
    )

    assert result.exit_code == 1
    assert "ambiguous_active_endpoint" in result.output
