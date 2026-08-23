from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "fleet" / "tailnet-browser-proxy.py"
SPEC = importlib.util.spec_from_file_location("tailnet_browser_proxy", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_tailnet_overlay_ranges_are_allowed() -> None:
    assert MODULE.is_tailnet_address("100.64.0.1")
    assert MODULE.is_tailnet_address("100.127.255.254")
    assert MODULE.is_tailnet_address("fd7a:115c:a1e0::1")


def test_non_tailnet_ranges_are_denied() -> None:
    assert not MODULE.is_tailnet_address("100.128.0.1")
    assert not MODULE.is_tailnet_address("10.0.0.1")
    assert not MODULE.is_tailnet_address("127.0.0.1")
    assert not MODULE.is_tailnet_address("not-an-ip")


def test_authority_parser_handles_names_ports_and_ipv6() -> None:
    assert MODULE.parse_authority("chiap08:8443", 443) == ("chiap08", 8443)
    assert MODULE.parse_authority("chiap08", 443) == ("chiap08", 443)
    assert MODULE.parse_authority("[fd7a:115c:a1e0::1]:443", 80) == (
        "fd7a:115c:a1e0::1",
        443,
    )


def test_pac_is_tailnet_scoped_and_direct_by_default() -> None:
    pac = MODULE.PAC_TEMPLATE.format(proxy_port=1055)
    assert 'dnsDomainIs(host, ".ts.net")' in pac
    assert 'isInNet(host, "100.64.0.0", "255.192.0.0")' in pac
    assert 'return "DIRECT"' in pac
