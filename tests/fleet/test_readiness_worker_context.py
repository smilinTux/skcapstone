"""Readiness must exercise bubblewrap in the real worker's host context."""

import configparser
from pathlib import Path

import pytest

from skcapstone.fleet import production_builder, production_tests

ROOT = Path(__file__).resolve().parents[2]
# These settings cause user-manager namespaces or change the privileges needed
# by the existing loaded-profile read. Defaults must agree with actual workers.
CONTEXT_DEFAULTS = {
    "PrivateTmp": "no",
    "PrivateUsers": "no",
    "PrivateMounts": "no",
    "PrivateDevices": "no",
    "ProtectSystem": "no",
    "ProtectHome": "no",
    "ReadWritePaths": "",
    "ReadOnlyPaths": "",
    "InaccessiblePaths": "",
    "BindPaths": "",
    "BindReadOnlyPaths": "",
    "TemporaryFileSystem": "",
    "RootDirectory": "",
    "RootImage": "",
    "NoNewPrivileges": "no",
}


def service(path):
    """Read the shipped unit without expanding systemd percent specifiers."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(path)
    return dict(parser["Service"])


@pytest.mark.parametrize(
    "template",
    ["systemd/skfleet-readiness.service", "systemd/production/skfleet-readiness.service"],
)
@pytest.mark.parametrize("worker_kind", ["builder", "qualification"])
def test_readiness_namespace_and_privilege_context_matches_actual_worker(
    template, worker_kind, tmp_path
):
    """Compare the shipped observer to generated real worker unit properties."""
    request = {
        "card_id": "a14e59a4",
        "request_id": "a" * 64,
        "production": {
            "resources": {
                "cpu_quota_percent": 100,
                "memory_max_bytes": 1073741824,
                "tasks_max": 64,
                "runtime_max_seconds": 60,
            }
        },
    }
    if worker_kind == "builder":
        command = production_builder.service_command(request, 1, ["/usr/bin/true"], tmp_path)
    else:
        command = production_tests.service_argv(
            request, tmp_path / "plan", tmp_path / "run", tmp_path
        )
    properties = dict(
        item.removeprefix("--property=").split("=", 1)
        for item in command
        if item.startswith("--property=")
    )
    observer = service(ROOT / template)
    for name, default in CONTEXT_DEFAULTS.items():
        assert observer.get(name, default) == properties.get(name, default), name
    assert observer["UMask"] == properties["UMask"] == "0077"
    assert observer["TimeoutStartSec"] == "120"
    assert "--python-bin %h/.skenv/bin/python3" in observer["ExecStart"]


def test_probe_keeps_real_sealed_executor_and_existing_failure_checks():
    """Correct the caller context without weakening the executed boundary."""
    source = (ROOT / "scripts/fleet/skfleet_readiness.py").read_text()
    worker = (ROOT / "src/skcapstone/fleet/production_test_worker.py").read_text()
    assert "subprocess.run(sandbox_command(source,output,['/usr/bin/true'])" in source
    assert '"--unshare-all"' in worker
    assert '"--ro-bind"' in worker
    assert '"--clearenv"' in worker
    assert "native sealed sandbox refused" in source
