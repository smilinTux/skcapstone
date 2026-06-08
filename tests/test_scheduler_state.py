import json
import threading
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


def test_concurrent_record_run_is_safe(tmp_path: Path):
    """Concurrent record_run calls from many threads must not corrupt state.json.

    With 8 threads each calling record_run 20 times (160 total writes), the
    resulting JSON file must still be valid and each job must show exactly 20
    successful runs.  This validates the _write_lock guard in SchedulerState.
    """
    st = SchedulerState(root=tmp_path, hostname="h")

    def worker(i: int) -> None:
        for _ in range(20):
            st.record_run(f"job{i}", ok=True)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    data = json.loads(st.state_file.read_text())
    assert len(data) == 8, f"expected 8 job keys, got {len(data)}: {list(data)}"
    for i in range(8):
        assert data[f"job{i}"]["run_count"] == 20, (
            f"job{i} run_count={data[f'job{i}']['run_count']}, expected 20"
        )
