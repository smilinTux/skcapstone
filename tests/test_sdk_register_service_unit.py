"""register_service records the systemd unit the service runs under.

Discovery matches a registry claim to a running unit by name. A service that
registers as ``capauth`` but runs as ``capauth-authz.service`` could never be
matched, so its registry entry read as declared_not_observed forever
(noroc2027, 2026-11-11). The unit is read from the process's own cgroup, so no
caller has to know or pass it.
"""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone import sdk


def _cgroup(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cgroup"
    path.write_text(text, encoding="utf-8")
    return path


def test_unit_is_read_from_a_user_service_cgroup(tmp_path: Path) -> None:
    path = _cgroup(
        tmp_path,
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/capauth-authz.service\n",
    )
    assert sdk._systemd_unit(path) == "capauth-authz.service"


def test_template_instance_units_keep_their_instance(tmp_path: Path) -> None:
    path = _cgroup(tmp_path, "0::/user.slice/user@1000.service/app.slice/skcapstone@lumina.service\n")
    assert sdk._systemd_unit(path) == "skcapstone@lumina.service"


def test_a_shell_or_session_scope_is_not_a_unit(tmp_path: Path) -> None:
    for text in (
        "0::/user.slice/user-1000.slice/session-4.scope\n",
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-tmux.scope\n",
        "0::/user.slice/user-1000.slice/user@1000.service/init.scope\n",
    ):
        assert sdk._systemd_unit(_cgroup(tmp_path, text)) is None


def test_unreadable_cgroup_means_no_unit(tmp_path: Path) -> None:
    assert sdk._systemd_unit(tmp_path / "missing") is None


def test_register_service_writes_the_unit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sdk, "_systemd_unit", lambda *a, **k: "capauth-authz.service")
    entry = json.loads(Path(sdk.register_service("capauth", home=tmp_path)).read_text())
    assert entry["systemd_unit"] == "capauth-authz.service"


def test_register_service_outside_systemd_writes_no_unit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sdk, "_systemd_unit", lambda *a, **k: None)
    entry = json.loads(Path(sdk.register_service("capauth", home=tmp_path)).read_text())
    assert "systemd_unit" not in entry
