"""Tests for the merged-versus-running fleet check.

The estate's most expensive unanswered question: is what we merged actually
what is running? Two measured incidents drive every assertion here:

- 2026-09-18: the lane-model-routing fix was merged while all five chi hosts
  kept running the pre-fix ``~/.local/bin/skfleet-rotate.py`` (md5 8c400694).
  Found only because a separate audit happened to look.
- skcoord 0.1.57: merged, released, PyPI-verified at 07:00, not installed
  until 23:45. Version strings lied the whole time: ``pip`` said 0.1.56 while
  the module said 0.1.0 on the same host.

So these tests hold the check to CONTENT (digests), to reporting a
split fleet as a distinct and worse finding than a uniformly-behind fleet,
and to never letting an unreachable host pass as OK.

All pure-function tests: ssh and git are injected or not needed, the same way
tests/test_skfleet_readiness.py exercises the readiness gate without a live
systemd.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from skfleet_merged_vs_running import (  # noqa: E402
    HostObservation,
    classify_artifact,
    diff_package_files,
    dispatcher_from_argv,
    exit_code_for,
    parse_exec_start_argv,
    render_artifact_lines,
    version_lie_lines,
)

# A real value measured on chiap01 2026-09-18: the unit FILE says
# /usr/bin/python3, but the drop-in seat-runtime-python.conf overrides
# ExecStart, and only `systemctl show -p ExecStart` reflects that. Parsing
# the unit file instead of this value would name the wrong interpreter.
CHIAP01_EXECSTART = (
    "{ path=/home/skuser01/.skenv/bin/python3 ; "
    "argv[]=/home/skuser01/.skenv/bin/python3 "
    "/home/skuser01/.local/bin/skfleet-rotate.py --go ; "
    "ignore_errors=no ; start_time=[Fri 2026-09-18 05:00:01 CDT] ; "
    "stop_time=[Fri 2026-09-18 05:00:50 CDT] ; pid=289586 ; "
    "code=exited ; status=0 }"
)


def test_parse_exec_start_honours_dropin_override():
    argv = parse_exec_start_argv(CHIAP01_EXECSTART)
    assert argv is not None
    assert argv[0] == "/home/skuser01/.skenv/bin/python3"
    assert argv[1] == "/home/skuser01/.local/bin/skfleet-rotate.py"
    assert argv[2] == "--go"


def test_parse_exec_start_returns_none_for_empty_or_garbage():
    assert parse_exec_start_argv("") is None
    assert parse_exec_start_argv("no braces here") is None


def test_dispatcher_from_argv_names_interpreter_and_script():
    interpreter, script = dispatcher_from_argv(
        [
            "/home/skuser01/.skenv/bin/python3",
            "/home/skuser01/.local/bin/skfleet-rotate.py",
            "--go",
        ]
    )
    assert interpreter == "/home/skuser01/.skenv/bin/python3"
    assert script == "/home/skuser01/.local/bin/skfleet-rotate.py"


def test_dispatcher_from_argv_handles_direct_script_execstart():
    interpreter, script = dispatcher_from_argv(["/home/x/.local/bin/skfleet-rotate.py", "--go"])
    assert interpreter is None
    assert script == "/home/x/.local/bin/skfleet-rotate.py"


# ---------------------------------------------------------------------------
# Required behaviour 1 and 2: drift when hashes differ, no drift when they
# match. Content, never a version string.
# ---------------------------------------------------------------------------


def test_drift_detected_when_hashes_differ():
    verdict = classify_artifact(
        "dispatcher",
        expected_digest="aaaa1111",
        observations=[HostObservation("chiap01", digest="bbbb2222")],
    )
    assert verdict.host_status["chiap01"] == "drift"
    assert verdict.state == "uniformly_behind"


def test_no_drift_when_hashes_match():
    verdict = classify_artifact(
        "dispatcher",
        expected_digest="aaaa1111",
        observations=[
            HostObservation("chiap01", digest="aaaa1111"),
            HostObservation("chiap02", digest="aaaa1111"),
        ],
    )
    assert verdict.state == "in_sync"
    assert verdict.host_status == {"chiap01": "ok", "chiap02": "ok"}


# ---------------------------------------------------------------------------
# Required behaviour 3: a split fleet is a DISTINCT and worse finding than a
# uniformly-behind fleet, and it names the hosts.
# ---------------------------------------------------------------------------


def test_split_fleet_reported_distinctly_from_uniformly_behind():
    split = classify_artifact(
        "dispatcher",
        expected_digest="cccc3333",
        observations=[
            HostObservation("chiap01", digest="aaaa1111"),
            HostObservation("chiap02", digest="aaaa1111"),
            HostObservation("chiap04", digest="bbbb2222"),
        ],
    )
    behind = classify_artifact(
        "dispatcher",
        expected_digest="cccc3333",
        observations=[
            HostObservation("chiap01", digest="aaaa1111"),
            HostObservation("chiap02", digest="aaaa1111"),
            HostObservation("chiap04", digest="aaaa1111"),
        ],
    )
    assert split.state == "split_fleet"
    assert behind.state == "uniformly_behind"
    assert split.state != behind.state
    # The split verdict must name which hosts disagree with which.
    assert split.groups == {
        "aaaa1111": ["chiap01", "chiap02"],
        "bbbb2222": ["chiap04"],
    }


def test_split_fleet_lines_name_the_hosts():
    verdict = classify_artifact(
        "dispatcher",
        expected_digest="cccc3333",
        observations=[
            HostObservation("chiap01", digest="aaaa1111"),
            HostObservation("chiap04", digest="bbbb2222"),
        ],
    )
    lines = render_artifact_lines(verdict)
    split_lines = [line for line in lines if line.startswith("SPLIT")]
    assert split_lines, lines
    assert "chiap01" in split_lines[0] and "chiap04" in split_lines[0]


def test_hosts_agreeing_with_each_other_and_with_merged_is_not_split():
    verdict = classify_artifact(
        "dispatcher",
        expected_digest="aaaa1111",
        observations=[
            HostObservation("chiap01", digest="aaaa1111"),
            HostObservation("chiap02", digest="aaaa1111"),
        ],
    )
    assert verdict.state == "in_sync"
    assert not [line for line in render_artifact_lines(verdict) if line.startswith("SPLIT")]


# ---------------------------------------------------------------------------
# Required behaviour 4: an unreachable host is UNKNOWN, never OK. Conflating
# those is the exact failure mode this check exists to end.
# ---------------------------------------------------------------------------


def test_unreachable_host_is_unknown_never_ok():
    verdict = classify_artifact(
        "dispatcher",
        expected_digest="aaaa1111",
        observations=[
            HostObservation("chiap01", digest="aaaa1111"),
            HostObservation("ziowk01", digest=None, error="ssh: connect timed out"),
        ],
    )
    assert verdict.host_status["ziowk01"] == "unknown"
    assert verdict.host_status["ziowk01"] != "ok"
    assert verdict.unknown == {"ziowk01": "ssh: connect timed out"}
    lines = render_artifact_lines(verdict)
    assert any(line.startswith("UNKNOWN") and "ziowk01" in line for line in lines)
    assert not any(line.startswith("OK") and "ziowk01" in line for line in lines)


def test_unknown_host_blocks_the_all_green_exit():
    measured_ok = classify_artifact(
        "dispatcher",
        expected_digest="aaaa1111",
        observations=[
            HostObservation("chiap01", digest="aaaa1111"),
            HostObservation("ziowk01", digest=None, error="unreachable"),
        ],
    )
    # Every measured host matches, but one host could not be measured:
    # that is UNCERTAIN (exit 2), never the all-green exit 0.
    assert exit_code_for([measured_ok]) == 2


def test_exit_codes_rank_drift_above_uncertainty():
    in_sync = classify_artifact(
        "a", expected_digest="x", observations=[HostObservation("h1", digest="x")]
    )
    behind = classify_artifact(
        "b", expected_digest="x", observations=[HostObservation("h1", digest="y")]
    )
    unknown = classify_artifact(
        "c", expected_digest="x", observations=[HostObservation("h1", digest=None, error="down")]
    )
    assert exit_code_for([in_sync]) == 0
    assert exit_code_for([in_sync, behind]) == 1
    assert exit_code_for([behind, unknown]) == 1
    assert exit_code_for([in_sync, unknown]) == 2


def test_no_observations_is_unmeasured_not_in_sync():
    verdict = classify_artifact("dispatcher", expected_digest="aaaa1111", observations=[])
    assert verdict.state == "unmeasured"
    assert exit_code_for([verdict]) == 2


# ---------------------------------------------------------------------------
# The library is compared per module file, so a drifted host names WHICH
# modules differ, not just that a digest changed.
# ---------------------------------------------------------------------------


def test_diff_package_files_names_changed_missing_and_extra():
    expected = {"card_store.py": "a1", "coordination.py": "b2", "gone.py": "c3"}
    found = {"card_store.py": "a1", "coordination.py": "ZZ", "new.py": "d4"}
    diff = diff_package_files(expected, found)
    assert diff["changed"] == ["coordination.py"]
    assert diff["missing"] == ["gone.py"]
    assert diff["extra"] == ["new.py"]


def test_diff_package_files_empty_when_identical():
    files = {"card_store.py": "a1"}
    diff = diff_package_files(files, dict(files))
    assert diff == {"changed": [], "missing": [], "extra": []}


# ---------------------------------------------------------------------------
# Version strings lie (skcoord: pip said 0.1.56, the module said 0.1.0 on
# the same host). They are reported as informational context and NEVER used
# for the verdict, but a disagreement between two version claims on one host
# is itself worth a warning line.
# ---------------------------------------------------------------------------


def test_version_disagreement_on_one_host_warns():
    lines = version_lie_lines("chiap01", pip_version="0.1.56", init_version="0.1.0")
    assert lines
    assert lines[0].startswith("WARN")
    assert "0.1.56" in lines[0] and "0.1.0" in lines[0]


def test_agreeing_versions_do_not_warn():
    assert version_lie_lines("chiap01", pip_version="0.1.56", init_version="0.1.56") == []
    assert version_lie_lines("chiap01", pip_version="0.1.56", init_version=None) == []
