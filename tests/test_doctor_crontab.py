"""Tests for the crontab missing-target doctor check (read-only)."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from skcapstone import cron_audit
from skcapstone.cron_audit import audit_crontab
from skcapstone.doctor import _check_crontab_targets


def _targets(text, home):
    return sorted(f.target for f in audit_crontab(text, home=home).findings)


def test_missing_and_present_scripts(tmp_path):
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/sh\n")
    text = f"0 7 * * * {ok}\n0 8 * * * {tmp_path}/gone.sh >> /dev/null 2>&1\n"
    assert _targets(text, tmp_path) == [str(tmp_path / "gone.sh")]


def test_comments_blank_and_env_lines_ignored(tmp_path):
    text = "# 0 7 * * * /nope/x.sh\n\nSHELL=/bin/bash\nMAILTO=a@b.c\n"
    audit = audit_crontab(text, home=tmp_path)
    assert audit.entries == 0 and not audit.findings


def test_reboot_and_home_expansion(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "up.sh").write_text("x")
    text = "@reboot ~/bin/up.sh\n@daily $HOME/bin/gone.sh\n@hourly ${HOME}/bin/up.sh\n"
    assert _targets(text, tmp_path) == [str(tmp_path / "bin" / "gone.sh")]


def test_cd_chain_relative_resolution(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "run.sh").write_text("x")
    text = f"* * * * * cd {d} && ./run.sh\n* * * * * cd {d} && ./missing.sh\n"
    assert _targets(text, tmp_path) == [str(d / "missing.sh")]


def test_cd_missing_directory_reported(tmp_path):
    assert _targets(f"* * * * * cd {tmp_path}/nodir && true\n", tmp_path) == [
        str(tmp_path / "nodir")
    ]


def test_wrapper_checks_wrapped_script_not_job_name(tmp_path):
    wrapper = tmp_path / "sk-cron-run"
    wrapper.write_text("x")
    script = tmp_path / "job.sh"
    script.write_text("x")
    good = f"* * * * * {wrapper} myjob {script} >> /dev/null 2>&1\n"
    assert _targets(good, tmp_path) == []
    bad = f"* * * * * {wrapper} myjob {tmp_path}/nojob.sh\n"
    assert _targets(bad, tmp_path) == [str(tmp_path / "nojob.sh")]


def test_env_prefix_and_interpreter_script(tmp_path):
    text = f"* * * * * FOO=1 PATH=/usr/bin:/bin /bin/bash {tmp_path}/gone.sh\n"
    assert _targets(text, tmp_path) == [str(tmp_path / "gone.sh")]


def test_bare_commands_use_crontab_path(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / "mytool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    with_path = f"PATH={bindir}:/usr/bin:/bin\n* * * * * mytool --x\n"
    assert _targets(with_path, tmp_path) == []
    assert _targets("* * * * * mytool --x\n* * * * * ls\n", tmp_path) == ["mytool"]


def test_percent_and_unknown_variable(tmp_path):
    audit = audit_crontab("* * * * * date +\\%F\n* * * * * $UNSET/x.sh\n", home=tmp_path)
    assert not audit.findings and audit.unresolved == 1


def test_doctor_no_crontab_passes():
    proc = subprocess.CompletedProcess([], 1, "", "no crontab for user")
    with patch.object(cron_audit.subprocess, "run", return_value=proc):
        (c,) = _check_crontab_targets()
    assert c.passed and not c.unknown


def test_doctor_crontab_binary_absent_is_unknown():
    with patch.object(cron_audit.subprocess, "run", side_effect=FileNotFoundError("crontab")):
        (c,) = _check_crontab_targets()
    assert c.unknown and c.passed


def test_doctor_reports_missing(tmp_path):
    proc = subprocess.CompletedProcess([], 0, f"0 7 * * * {tmp_path}/gone.sh\n", "")
    with patch.object(cron_audit.subprocess, "run", return_value=proc):
        (c,) = _check_crontab_targets()
    assert not c.passed and "gone.sh" in c.detail and c.category == "cron"


def test_wrapper_option_values_not_flagged(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("x")
    text = (
        f"* * * * * nice -n 10 {script}\n"
        f"* * * * * timeout -s KILL 10 {script}\n"
        f"* * * * * flock {tmp_path}/lock -c '{script}'\n"
        f"* * * * * flock -n {tmp_path}/lock {script}\n"
    )
    assert _targets(text, tmp_path) == []


def test_wrapper_option_values_still_catch_missing(tmp_path):
    text = f"* * * * * nice -n 10 {tmp_path}/gone.sh\n* * * * * flock l -c '{tmp_path}/gone2.sh'\n"
    assert _targets(text, tmp_path) == [str(tmp_path / "gone.sh"), str(tmp_path / "gone2.sh")]


def test_subshell_parens_not_flagged(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "run.sh").write_text("x")
    assert _targets(f"* * * * * (cd {d} && ./run.sh)\n", tmp_path) == []
    assert _targets(f"* * * * * (cd {d} && ./nope.sh)\n", tmp_path) == [str(d / "nope.sh")]
