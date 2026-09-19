"""Tests for multi-address and role-aware SKGateway CMDB discovery."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner
from skcoord.cmdb import CMDBManager

from skcapstone.cli import main
from skcapstone.cmdb_discovery import (
    collect_declared_skgateway_ingress,
    collect_listening_ports,
    collect_systemd_units,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cmdb" / "chiap01-skgateway-ingress.json"


@dataclass
class SyntheticRunner:
    """Return exact synthetic command output without contacting a host."""

    host: str = "chiap01"
    outputs: dict[tuple[str, ...], str | None] = field(default_factory=dict)

    def run(self, argv):
        """Return the configured output for one exact argument vector."""

        key = tuple(argv)
        if key == ("cat", "/proc/sys/net/ipv4/ip_local_port_range"):
            return "32768 60999"
        if key[:3] == ("systemctl", "--system", "show"):
            if "FragmentPath" in key:
                return self.outputs.get(("fragment-paths",))
            if "Requires" in key:
                return self.outputs.get(("dependencies",))
        return self.outputs.get(key)


def _ss_line(address: str, process: str) -> str:
    return f"LISTEN 0 4096 {address}:28880 0.0.0.0:* " f'users:(("{process}",pid=100,fd=3))'


def test_same_port_preserves_sorted_exact_address_set_and_stable_id() -> None:
    lines = [
        _ss_line("192.168.50.11", "proxy"),
        _ss_line("127.0.0.1", "gateway"),
        _ss_line("100.91.152.43", "proxy"),
        _ss_line("127.0.0.1", "gateway"),
    ]
    first = SyntheticRunner(outputs={("ss", "-tlnpH"): "\n".join(lines)})
    second = SyntheticRunner(outputs={("ss", "-tlnpH"): "\n".join(reversed(lines))})

    one = collect_listening_ports(first)
    two = collect_listening_ports(second)

    assert len(one) == 1
    assert one == two
    assert one[0].ci_id == "ci-port-chiap01-28880"
    assert one[0].attributes == {
        "port": 28880,
        "proto": "tcp",
        "bind_addresses": ["100.91.152.43", "127.0.0.1", "192.168.50.11"],
        "processes": ["gateway", "proxy"],
    }


def test_single_address_retains_legacy_scalar_attributes() -> None:
    runner = SyntheticRunner(outputs={("ss", "-tlnpH"): _ss_line("127.0.0.1", "gateway")})
    item = collect_listening_ports(runner)[0]
    assert item.attributes["bind"] == "127.0.0.1"
    assert item.attributes["process"] == "gateway"


def test_exact_systemd_units_keep_distinct_role_aware_identities() -> None:
    gateway = "skgateway-shared.service"
    socket = "skgateway-shared.socket"
    proxy = "skgateway-ingress-proxy.service"
    firewall = "skgateway-ingress-firewall.service"
    units = {
        (
            "systemctl",
            "--system",
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--no-pager",
            "--plain",
        ): (
            "skgateway-shared.service loaded active running\n"
            "skgateway-ingress-proxy.service loaded active running\n"
            "skgateway-ingress-firewall.service loaded active exited"
        ),
        (
            "systemctl",
            "--system",
            "list-units",
            "--type=socket",
            "--all",
            "--no-legend",
            "--no-pager",
            "--plain",
        ): ("skgateway-shared.socket loaded active listening"),
        (
            "systemctl",
            "--system",
            "list-units",
            "--type=timer",
            "--all",
            "--no-legend",
            "--no-pager",
            "--plain",
        ): "",
        ("fragment-paths",): "\n\n".join(
            f"Id={unit}\nFragmentPath=/etc/systemd/system/{unit}"
            for unit in (gateway, proxy, firewall, socket)
        ),
        ("dependencies",): (
            f"Id={gateway}\nRequires=\nWants=\n\n"
            f"Id={proxy}\nRequires={gateway}\nWants=\n\n"
            f"Id={firewall}\nRequires={proxy}\nWants=\n\n"
            f"Id={socket}\nRequires={gateway}\nWants="
        ),
    }
    found = collect_systemd_units(SyntheticRunner(outputs=units), scopes=("--system",))
    by_role = {item.attributes["unit_role"]: item for item in found}

    assert set(by_role) == {"gateway", "socket", "proxy", "firewall"}
    assert len({item.ci_id for item in found}) == 4
    assert by_role["gateway"].ci_id != by_role["socket"].ci_id
    assert by_role["gateway"].attributes["systemd_unit"] == gateway
    assert by_role["socket"].attributes["systemd_unit"] == socket
    assert any(rel == "activates" for rel, _ in by_role["socket"].relationships)
    assert any(rel == "proxies_to" for rel, _ in by_role["proxy"].relationships)
    assert any(rel == "protects" for rel, _ in by_role["firewall"].relationships)


def test_generic_systemd_service_preserves_backward_compatible_identity() -> None:
    outputs = {
        (
            "systemctl",
            "--system",
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--no-pager",
            "--plain",
        ): "sshd.service loaded active running",
        ("fragment-paths",): (
            "Id=sshd.service\nFragmentPath=/usr/lib/systemd/system/sshd.service"
        ),
        ("dependencies",): "Id=sshd.service\nRequires=\nWants=",
    }
    item = collect_systemd_units(
        SyntheticRunner(outputs=outputs),
        scopes=("--system",),
        kinds=("service",),
    )[0]
    assert item.name == "sshd"
    assert item.ci_id == "ci-service-sshd"
    assert item.attributes["unit_role"] == "service"


def _install_fixture(home: Path) -> Path:
    target = home / "config" / "cmdb" / "skgateway-ingress.json"
    target.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, target)
    return target


def test_chiap01_28880_declaration_has_exact_port_units_and_relationships(
    tmp_path: Path,
) -> None:
    _install_fixture(tmp_path)
    found = collect_declared_skgateway_ingress(tmp_path)

    assert len(found) == 5
    port = next(item for item in found if item.ci_type == "port")
    assert port.ci_id == "ci-port-chiap01-28880"
    assert port.attributes["bind_addresses"] == [
        "100.91.152.43",
        "127.0.0.1",
        "192.168.50.11",
    ]
    assert {item.attributes["unit_role"] for item in found if item.ci_type == "service"} == {
        "gateway",
        "socket",
        "proxy",
        "firewall",
    }
    assert len({item.ci_id for item in found}) == 5
    targets = {target for item in found for _, target in item.relationships}
    assert "ci-service-chiap01-system-skgateway-shared.service" in targets
    assert "ci-service-chiap01-system-skgateway-ingress-proxy.service" in targets


@pytest.mark.parametrize(
    ("address", "boundary"),
    [
        ("0.0.0.0", "lan"),
        ("::", "lan"),
        ("*", "lan"),
        ("8.8.8.8", "lan"),
        ("169.254.1.1", "lan"),
        ("224.0.0.1", "lan"),
        ("172.17.0.1", "lan"),
        ("192.168.50.11", "tailscale"),
        ("100.91.152.43", "lan"),
        ("127.0.0.1%lo", "loopback"),
    ],
)
def test_unsafe_bind_fails_closed_without_partial_output(
    tmp_path: Path, address: str, boundary: str
) -> None:
    target = _install_fixture(tmp_path)
    payload = json.loads(target.read_text())
    payload["profiles"][0]["binds"][0] = {
        "address": address,
        "boundary": boundary,
    }
    target.write_text(json.dumps(payload))

    with pytest.raises(ValueError):
        collect_declared_skgateway_ingress(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"extra": True}),
        lambda payload: payload["profiles"][0].update({"api_key": "not-a-key"}),
        lambda payload: payload["profiles"][0].update({"profile_id": "chiap01.api-token"}),
        lambda payload: payload["profiles"][0]["units"][0].update(
            {"targets": ["unknown.service"]}
        ),
        lambda payload: payload["profiles"][0]["units"].pop(),
        lambda payload: payload["profiles"].append(payload["profiles"][0].copy()),
    ],
)
def test_malformed_or_secret_looking_declaration_fails_closed(tmp_path: Path, mutation) -> None:
    target = _install_fixture(tmp_path)
    payload = json.loads(target.read_text())
    mutation(payload)
    target.write_text(json.dumps(payload))

    with pytest.raises(ValueError):
        collect_declared_skgateway_ingress(tmp_path)


def test_cli_dry_run_fixture_is_deterministic_and_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fixture(tmp_path)
    monkeypatch.setattr("skcapstone.cli.cmdb.SHARED_ROOT", str(tmp_path))

    first = CliRunner().invoke(main, ["cmdb", "scan", "--no-local", "--json"])
    second = CliRunner().invoke(main, ["cmdb", "scan", "--no-local", "--json"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    one, two = json.loads(first.output), json.loads(second.output)
    for item in one:
        item.pop("scan_id", None)
        item.pop("observed_at", None)
    for item in two:
        item.pop("scan_id", None)
        item.pop("observed_at", None)
    assert one == two
    assert len(one) == 5
    assert CMDBManager(tmp_path).list_cis() == []


def test_network_orchestration_uses_the_same_extended_collectors() -> None:
    from skcapstone.cli.cmdb import _orchestration

    orchestration = _orchestration()
    declared = {collector.__name__ for collector in orchestration.DECLARED_COLLECTORS}
    observed = {collector.__name__: collector for collector in orchestration.OBSERVED_COLLECTORS}
    assert "collect_declared_skgateway_ingress" in declared
    assert observed["collect_listening_ports"] is collect_listening_ports
    assert observed["collect_systemd_units"] is collect_systemd_units
