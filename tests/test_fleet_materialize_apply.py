from pathlib import Path

from skcapstone.fleet.materialize_apply import apply_unit, remove_unit


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()


SPEC = {
    "unit": "demo.service",
    "runtime": "systemd-user",
    "exec": "%h/.skenv/bin/python3 -c pass",
    "type": "oneshot",
    "schedule": "5m",
}


def test_apply_writes_service_and_timer_then_reloads(tmp_path):
    r = FakeRunner()
    written = apply_unit("demo", SPEC, unit_dir=tmp_path, runner=r)
    assert sorted(Path(p).name for p in written) == ["demo.service", "demo.timer"]
    assert (tmp_path / "demo.service").read_text().startswith("# Managed by")
    assert "daemon-reload" in " ".join(" ".join(c) for c in r.calls)


def test_apply_is_idempotent(tmp_path):
    apply_unit("demo", SPEC, unit_dir=tmp_path, runner=FakeRunner())
    r2 = FakeRunner()
    assert apply_unit("demo", SPEC, unit_dir=tmp_path, runner=r2) == []
    # Nothing changed, so nothing is reloaded.
    assert r2.calls == []


def test_apply_rewrites_when_spec_changes(tmp_path):
    apply_unit("demo", SPEC, unit_dir=tmp_path, runner=FakeRunner())
    changed = dict(SPEC, exec="%h/.skenv/bin/python3 -c other")
    assert apply_unit("demo", changed, unit_dir=tmp_path, runner=FakeRunner())


def test_apply_dry_run_writes_nothing(tmp_path):
    r = FakeRunner()
    planned = apply_unit("demo", SPEC, unit_dir=tmp_path, runner=r, dry_run=True)
    assert planned
    assert not list(tmp_path.iterdir())
    assert r.calls == []


def test_apply_without_schedule_writes_only_service(tmp_path):
    spec = {k: v for k, v in SPEC.items() if k != "schedule"}
    written = apply_unit("demo", spec, unit_dir=tmp_path, runner=FakeRunner())
    assert [Path(p).name for p in written] == ["demo.service"]


def test_remove_unit_deletes_and_reloads(tmp_path):
    apply_unit("demo", SPEC, unit_dir=tmp_path, runner=FakeRunner())
    r = FakeRunner()
    removed = remove_unit("demo", unit_dir=tmp_path, runner=r)
    assert sorted(Path(p).name for p in removed) == ["demo.service", "demo.timer"]
    assert not list(tmp_path.iterdir())
    joined = " ".join(" ".join(c) for c in r.calls)
    assert "disable" in joined and "daemon-reload" in joined
