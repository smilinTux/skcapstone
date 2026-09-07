"""Tests for multi-address and role-aware SKGateway CMDB discovery."""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner
from skcoord.cmdb import CMDBManager
from skcoord.discovery_systemd import (
    collect_systemd_units as collect_upstream_systemd_units,
)

from skcapstone.cli import main
from skcapstone.cmdb_discovery import (
    collect_declared_skgateway_ingress,
    collect_listening_ports,
    collect_systemd_units,
    scan,
)
from skcapstone.cmdb_ingress_declaration import systemd_service_identity

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
    assert any(rel == "depends_on" for rel, _ in by_role["proxy"].relationships)
    assert any(rel == "depends_on" for rel, _ in by_role["firewall"].relationships)
    assert not any(rel == "proxies_to" for rel, _ in by_role["proxy"].relationships)
    assert not any(rel == "protects" for rel, _ in by_role["firewall"].relationships)


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


@pytest.mark.parametrize(
    "unit",
    ["ordinary-gateway.service", "tailscale-proxy.service", "host-firewall.service"],
)
def test_role_like_service_names_preserve_legacy_identity(unit: str) -> None:
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
        ): f"{unit} loaded active running",
        ("fragment-paths",): f"Id={unit}\nFragmentPath=/etc/systemd/system/{unit}",
        ("dependencies",): f"Id={unit}\nRequires=\nWants=",
    }
    item = collect_systemd_units(
        SyntheticRunner(outputs=outputs), scopes=("--system",), kinds=("service",)
    )[0]
    assert item.name == unit.removesuffix(".service")


def test_long_observed_services_preserve_upstream_identity_bytes() -> None:
    names = (
        "ordinary-legacy-service-with-a-name-that-is-definitely-long-alpha.service",
        "ordinary-legacy-service-with-a-name-that-is-definitely-long-beta.service",
    )
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
        ): "\n".join(f"{unit} loaded active running" for unit in names),
        ("fragment-paths",): "\n\n".join(
            f"Id={unit}\nFragmentPath=/etc/systemd/system/{unit}" for unit in names
        ),
        ("dependencies",): "\n\n".join(f"Id={unit}\nRequires=\nWants=" for unit in names),
    }
    runner = SyntheticRunner(outputs=outputs)
    found = collect_systemd_units(runner, scopes=("--system",), kinds=("service",))
    upstream = collect_upstream_systemd_units(runner, scopes=("--system",), kinds=("service",))

    def identity(item):
        return item.name, item.canonical_name, item.ci_id

    assert sorted(map(identity, found)) == sorted(map(identity, upstream))
    assert [item.name for item in found] == [unit.removesuffix(".service") for unit in names]


@pytest.mark.parametrize("kind", ["socket", "timer"])
def test_long_observed_nonservice_identities_are_collision_resistant(kind: str) -> None:
    names = (
        f"governed-nonservice-with-a-shared-prefix-that-is-definitely-long-alpha.{kind}",
        f"governed-nonservice-with-a-shared-prefix-that-is-definitely-long-beta.{kind}",
    )
    outputs = {
        (
            "systemctl",
            "--system",
            "list-units",
            f"--type={kind}",
            "--all",
            "--no-legend",
            "--no-pager",
            "--plain",
        ): "\n".join(f"{unit} loaded active running" for unit in names),
        ("fragment-paths",): "\n\n".join(
            f"Id={unit}\nFragmentPath=/etc/systemd/system/{unit}" for unit in names
        ),
        ("dependencies",): "\n\n".join(f"Id={unit}\nRequires=\nWants=" for unit in names),
    }
    runner = SyntheticRunner(outputs=outputs)

    first = collect_systemd_units(runner, scopes=("--system",), kinds=(kind,))
    second = collect_systemd_units(runner, scopes=("--system",), kinds=(kind,))

    expected = {systemd_service_identity("chiap01", "system", unit) for unit in names}
    assert first == second
    assert {item.name for item in first} == expected
    assert len({item.ci_id for item in first}) == 2
    assert all(item.attributes["systemd_kind"] == kind for item in first)


def test_governed_long_unit_identities_remain_collision_resistant(tmp_path: Path) -> None:
    target = _install_fixture(tmp_path)
    payload = json.loads(target.read_text())
    gateway = "skgateway-governed-unit-with-a-shared-prefix-that-exceeds-forty-eight-alpha.service"
    proxy = "skgateway-governed-unit-with-a-shared-prefix-that-exceeds-forty-eight-beta.service"
    units = {item["role"]: item for item in payload["profiles"][0]["units"]}
    units["gateway"]["unit"] = gateway
    units["socket"]["targets"] = [gateway]
    units["proxy"]["unit"] = proxy
    units["proxy"]["targets"] = [gateway]
    units["firewall"]["targets"] = [proxy]
    target.write_text(json.dumps(payload))

    declared = collect_declared_skgateway_ingress(tmp_path)
    governed = [
        item
        for item in declared
        if item.ci_type == "service" and set(item.aliases) & {gateway, proxy}
    ]

    assert len(governed) == 2
    assert {item.aliases[0] for item in governed} == {gateway, proxy}
    assert len({item.name for item in governed}) == 2
    assert len({item.ci_id for item in governed}) == 2


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
        "10.0.0.223",
        "10.0.0.47",
        "100.80.180.78",
        "127.0.0.1",
    ]
    assert {item.attributes["unit_role"] for item in found if item.ci_type == "service"} == {
        "gateway",
        "socket",
        "proxy",
        "firewall",
    }
    assert len({item.ci_id for item in found}) == 5
    targets = {target for item in found for _, target in item.relationships}
    assert "ci-service-skgateway-shared" in targets
    assert "ci-service-skgateway-ingress-proxy" in targets
    binders = [
        item.attributes["unit_role"]
        for item in found
        if any(rel == "binds" for rel, _ in item.relationships)
    ]
    assert binders == ["socket"]


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


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["profiles"][0]["binds"].append(
            payload["profiles"][0]["binds"][0].copy()
        ),
        lambda payload: payload["profiles"][0]["units"][1].update(
            {"targets": ["skgateway-ingress-firewall.service"]}
        ),
        lambda payload: payload["profiles"][0]["units"][1].update(
            {"targets": ["skgateway-shared.socket"]}
        ),
        lambda payload: payload["profiles"][0]["units"][1].update(
            {"targets": ["skgateway-shared.service", "skgateway-shared.service"]}
        ),
        lambda payload: payload["profiles"][0]["units"][1].update(
            {"unit": "skgateway-shared.service"}
        ),
    ],
)
def test_ambiguous_bind_or_role_graph_fails_closed(tmp_path: Path, mutation) -> None:
    target = _install_fixture(tmp_path)
    payload = json.loads(target.read_text())
    mutation(payload)
    target.write_text(json.dumps(payload))

    with pytest.raises(ValueError):
        collect_declared_skgateway_ingress(tmp_path)


@pytest.mark.parametrize("reuse_endpoint", [True, False])
def test_profiles_cannot_reuse_endpoint_or_unit_ownership(
    tmp_path: Path, reuse_endpoint: bool
) -> None:
    target = _install_fixture(tmp_path)
    payload = json.loads(target.read_text())
    duplicate = copy.deepcopy(payload["profiles"][0])
    duplicate["profile_id"] = "chiap01.other.skgateway.v1"
    if not reuse_endpoint:
        duplicate["port"] = 28881
    payload["profiles"].append(duplicate)
    target.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="already owned"):
        collect_declared_skgateway_ingress(tmp_path)


def test_malformed_governed_declaration_aborts_scan_and_cli(tmp_path: Path) -> None:
    target = _install_fixture(tmp_path)
    payload = json.loads(target.read_text())
    payload["profiles"][0]["binds"].pop()
    target.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="boundaries are incomplete"):
        scan(tmp_path, runners=())

    from skcapstone.cli import cmdb as cmdb_cli

    original = cmdb_cli.SHARED_ROOT
    cmdb_cli.SHARED_ROOT = str(tmp_path)
    try:
        result = CliRunner().invoke(main, ["cmdb", "scan", "--no-local", "--json"])
    finally:
        cmdb_cli.SHARED_ROOT = original
    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)


def test_malformed_governed_declaration_aborts_network_preflight(tmp_path: Path) -> None:
    target = _install_fixture(tmp_path)
    payload = json.loads(target.read_text())
    payload["profiles"][0]["binds"].pop()
    target.write_text(json.dumps(payload))

    from skcapstone.cli.cmdb import _orchestration

    with pytest.raises(ValueError, match="boundaries are incomplete"):
        _orchestration().scan_network(tmp_path, (), lambda _host: None)


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
