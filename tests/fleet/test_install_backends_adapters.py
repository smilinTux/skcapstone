"""Tests for the default backend adapters that shell out to per-repo installers."""
from skcapstone.fleet.install_backends import default_backends


class _FakeRunner:
    """Records invocations and returns a canned CompletedProcess-like result."""

    def __init__(self, rc=0, stderr=""):
        self.calls = []
        self.rc = rc
        self.stderr = stderr

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)

        class R:
            pass

        R.returncode = self.rc
        R.stderr = self.stderr
        R.stdout = ""
        return R


def test_dry_run_reports_would_write_and_passes_dry_run_flag():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    status, detail = b["skchat"](
        ["skchat-webui@lumina.service"], dry_run=True, enable=False, start=False
    )
    assert status == "would-write"
    assert any("install.sh" in " ".join(map(str, c)) for c in runner.calls)


def test_nonzero_runner_is_failed_with_stderr_tail():
    runner = _FakeRunner(rc=1, stderr="boom")
    b = default_backends(runner=runner)
    status, detail = b["packages"](["capauth"], dry_run=False, enable=False, start=False)
    assert status == "failed" and "boom" in detail


def test_successful_run_is_ok_with_no_detail():
    runner = _FakeRunner(rc=0)
    b = default_backends(runner=runner)
    status, detail = b["skcomms"](["skcomms.service"], dry_run=False, enable=False, start=False)
    assert status == "ok"
    assert detail == ""


def test_agent_backend_dispatches_skwhisper_units_to_skwhisper_cli():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["agent"](["skwhisper@lumina.service"], dry_run=False, enable=False, start=False)
    cmd = runner.calls[0]
    assert cmd[:4] == ["skwhisper", "install", "--agent", "lumina"]


def test_agent_backend_dispatches_skmemory_units_to_install_systemd_script():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["agent"](["skmemory-embed@lumina.service"], dry_run=False, enable=False, start=False)
    cmd = " ".join(map(str, runner.calls[0]))
    assert "install-systemd.sh" in cmd


def test_core_backend_enables_each_unit_via_systemctl_when_enable_set():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    status, detail = b["core"](["skgateway.service"], dry_run=False, enable=True, start=False)
    assert status == "ok"
    assert ["systemctl", "--user", "enable", "skgateway.service"] in runner.calls


def test_core_backend_skips_systemctl_enable_in_dry_run():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    status, _ = b["core"](["skgateway.service"], dry_run=True, enable=True, start=False)
    assert status == "would-write"
    assert not any(c[0] == "systemctl" for c in runner.calls)


def test_capauth_authz_backend_shells_to_capauth_deploy_script():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["capauth-authz"](["capauth-authz.service"], dry_run=False, enable=False, start=False)
    cmd = " ".join(map(str, runner.calls[0]))
    assert "capauth/deploy/capauth-service/deploy.sh" in cmd


def test_skchat_backend_passes_enable_and_start_flags_when_not_dry_run():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["skchat"](["skchat-webui@lumina.service"], dry_run=False, enable=True, start=True)
    cmd = runner.calls[0]
    assert "--enable" in cmd
    assert "--start" in cmd
    assert "--diff" not in cmd


def test_repos_root_honors_skcapstone_repos_env_override(monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_REPOS", "/opt/custom-repos")
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["packages"](["capauth"], dry_run=False, enable=False, start=False)
    cmd = " ".join(map(str, runner.calls[0]))
    assert "/opt/custom-repos/skcapstone/scripts/install.sh" in cmd
