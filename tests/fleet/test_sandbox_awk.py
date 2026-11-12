"""Expose the system awk alternative without exposing host /etc."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skcapstone.fleet import production_test_worker as worker


def alternative(monkeypatch, target="/usr/bin/mawk", regular=True):
    awk = MagicMock(spec=Path)
    awk.is_symlink.return_value = True
    awk.readlink.return_value = Path("/etc/alternatives/awk")
    resolved = MagicMock(spec=Path)
    resolved.parent = Path(target).parent
    resolved.is_file.return_value = regular
    resolved.__str__.return_value = target
    awk.resolve.return_value = resolved
    monkeypatch.setattr(
        worker, "Path", lambda value: awk if value == "/usr/bin/awk" else Path(value)
    )
    return awk


@pytest.mark.parametrize("target", ["/usr/bin/mawk", "/usr/bin/gawk"])
def test_awk_alternative_uses_only_already_sealed_system_binary(monkeypatch, target):
    awk = alternative(monkeypatch, target)
    assert worker.system_awk_alias() == ["--symlink", target, "/etc/alternatives/awk"]
    awk.resolve.assert_called_once_with(strict=True)


def test_direct_awk_needs_no_alias(monkeypatch):
    awk = alternative(monkeypatch)
    awk.is_symlink.return_value = False
    assert worker.system_awk_alias() == []
    awk.resolve.assert_not_called()


def test_symlink_within_usr_needs_no_alias(monkeypatch):
    awk = alternative(monkeypatch)
    awk.readlink.return_value = Path("/usr/bin/mawk")
    assert worker.system_awk_alias() == []


@pytest.mark.parametrize("target", ["/etc/passwd", "/home/operator/awk"])
def test_alternative_outside_system_bin_fails_closed(monkeypatch, target):
    alternative(monkeypatch, target)
    with pytest.raises(worker.TestEvidenceError, match="system awk"):
        worker.system_awk_alias()


def test_non_file_alternative_fails_closed(monkeypatch):
    alternative(monkeypatch, regular=False)
    with pytest.raises(worker.TestEvidenceError, match="system awk"):
        worker.system_awk_alias()


def test_broken_alternative_fails_closed(monkeypatch):
    awk = alternative(monkeypatch)
    awk.resolve.side_effect = FileNotFoundError
    with pytest.raises(worker.TestEvidenceError, match="system awk"):
        worker.system_awk_alias()


def test_sandbox_recreates_only_awk_link_and_keeps_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        worker,
        "system_awk_alias",
        lambda: ["--symlink", "/usr/bin/mawk", "/etc/alternatives/awk"],
    )
    command = worker.sandbox_command(tmp_path / "source", tmp_path / "output", ["awk"])
    index = command.index("/etc/alternatives/awk")
    assert command[index - 2 : index + 1] == [
        "--symlink",
        "/usr/bin/mawk",
        "/etc/alternatives/awk",
    ]
    assert "--unshare-all" in command and "--clearenv" in command
    assert command.count("--bind") == 1
    assert "/etc" not in command and "/etc/alternatives" not in command
    assert command[command.index(str(tmp_path / "source")) - 1] == "--ro-bind"
