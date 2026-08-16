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
    # The would-write detail still names the command that would have run...
    assert "install.sh" in detail
    # ...but skchat/systemd/install.sh MUST NOT actually be invoked in dry-run.
    assert runner.calls == []


def test_dry_run_never_invokes_runner():
    # CRITICAL: several real installers (e.g. skcapstone/scripts/install.sh)
    # silently ignore unrecognized flags like --dry-run and perform a REAL
    # install. The only safe contract is: dry_run=True never calls runner,
    # for every backend.
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    cases = [
        ("packages", ["capauth"]),
        ("skchat", ["skchat-webui@lumina.service"]),
        ("skcomms", ["skcomms.service"]),
        ("core", ["skgateway.service"]),
        ("agent", ["skwhisper@lumina.service"]),
        ("agent", ["skmemory-embed@lumina.service"]),
        ("capauth-authz", ["capauth-authz.service"]),
    ]
    for backend_id, names in cases:
        status, _ = b[backend_id](names, dry_run=True, enable=True, start=True)
        assert status == "would-write", backend_id
    assert runner.calls == []


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


def test_packages_backend_passes_non_interactive_flag_and_no_other_flags():
    # skcapstone/scripts/install.sh recognizes --dev/--force/--non-interactive;
    # packages always passes --non-interactive (copy-vs-activate: venv + pip
    # install only, never systemd), and no --dry-run/--enable/--start belongs
    # on this argv.
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["packages"](["capauth"], dry_run=False, enable=True, start=True)
    cmd = runner.calls[0]
    assert cmd[-2].endswith("install.sh")
    assert cmd[-1] == "--non-interactive"
    assert not any(a.startswith("--") and a != "--non-interactive" for a in cmd)


def test_agent_backend_dispatches_skwhisper_units_to_skwhisper_cli():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["agent"](["skwhisper@lumina.service"], dry_run=False, enable=False, start=False)
    cmd = runner.calls[0]
    assert cmd == ["skwhisper", "install", "--agent", "lumina"]


def test_agent_backend_skwhisper_passes_start_flag_only_when_set():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["agent"](["skwhisper@lumina.service"], dry_run=False, enable=False, start=True)
    cmd = runner.calls[0]
    assert cmd == ["skwhisper", "install", "--agent", "lumina", "--start"]


def test_agent_backend_dispatches_skmemory_units_to_install_systemd_script_with_agents_flag():
    # install-systemd.sh hits an interactive `read` prompt and aborts under
    # subprocess unless --agents is passed for non-interactive mode.
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["agent"](["skmemory-embed@lumina.service"], dry_run=False, enable=False, start=False)
    cmd = runner.calls[0]
    assert cmd[-2:] == ["--agents", "lumina"]
    assert "install-systemd.sh" in " ".join(map(str, cmd))
    assert not any(a in ("--dry-run", "--enable", "--start") for a in cmd)


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
    assert runner.calls == []


def test_skcomms_backend_passes_only_no_service_flag():
    # bootstrap.sh exits 2 on any argument other than --no-service.
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["skcomms"](["skcomms.service"], dry_run=False, enable=False, start=False)
    cmd = runner.calls[0]
    assert cmd[-1] == "--no-service"
    assert not any(a in ("--dry-run", "--enable", "--start") for a in cmd)


def test_skcomms_backend_enables_units_via_systemctl_when_enable_set():
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    status, _ = b["skcomms"](["skcomms.service"], dry_run=False, enable=True, start=False)
    assert status == "ok"
    assert ["systemctl", "--user", "enable", "skcomms.service"] in runner.calls


def test_capauth_authz_backend_shells_to_capauth_deploy_script_with_no_flags():
    # deploy.sh's $1 is a positional MODE (--test|--stop|--status|--provision|"");
    # it does not accept --dry-run/--enable/--start.
    runner = _FakeRunner()
    b = default_backends(runner=runner)
    b["capauth-authz"](["capauth-authz.service"], dry_run=False, enable=False, start=False)
    cmd = runner.calls[0]
    assert cmd[-1].endswith("capauth/deploy/capauth-service/deploy.sh")
    assert len(cmd) == 2


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
