from datetime import datetime, timezone
from pathlib import Path
from skcapstone.scheduler_state import SchedulerState


def test_state_roundtrip(tmp_path: Path):
    st = SchedulerState(root=tmp_path, hostname="hostA")
    assert st.last_run("job1") is None
    now = datetime(2026, 6, 8, 6, 0, tzinfo=timezone.utc)
    st.record_run("job1", now=now, ok=True)
    st2 = SchedulerState(root=tmp_path, hostname="hostA")
    assert st2.last_run("job1") == now
    rec = st2.get("job1")
    assert rec["run_count"] == 1 and rec["error_count"] == 0


def test_state_path_is_host_scoped(tmp_path: Path):
    st = SchedulerState(root=tmp_path, hostname="hostA")
    assert st.state_file == tmp_path / "scheduler" / "hostA" / "state.json"


def test_error_run_increments_error_count(tmp_path: Path):
    st = SchedulerState(root=tmp_path, hostname="hostA")
    st.record_run("j", ok=False, error="boom")
    rec = st.get("j")
    assert rec["error_count"] == 1 and rec["run_count"] == 0
    assert rec["last_status"] == "error" and rec["last_error"] == "boom"
