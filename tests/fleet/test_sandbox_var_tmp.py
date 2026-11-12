"""Conventional restore scratch paths share the sandbox's private temporary tree."""

from skcapstone.fleet import production_test_worker as worker


def test_var_tmp_alias_uses_private_tmp_and_preserves_isolation(tmp_path):
    command = worker.sandbox_command(tmp_path / "source", tmp_path / "output", ["true"])
    index = command.index("/var/tmp")
    assert command[index - 2 : index + 1] == ["--symlink", "/tmp", "/var/tmp"]
    assert ["--tmpfs", "/tmp"] == command[command.index("/tmp") - 1 : command.index("/tmp") + 1]
    assert command.count("--tmpfs") == 1
    assert command.count("--bind") == 1
    assert "/var" not in command and "/etc" not in command
    assert "--unshare-all" in command and "--clearenv" in command
    assert command[command.index(str(tmp_path / "source")) - 1] == "--ro-bind"
