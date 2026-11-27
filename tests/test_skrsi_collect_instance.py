"""SKRSI instance evidence collector: readers, missingness, append only, fail closed."""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "skrsi" / "collect_instance.py"
DAY = "2026-11-13"

FAKE_CLUSTER_CONFIG = """
import json
def load_cluster_yaml(text):
    return json.loads(text)
"""

# Mirrors the framework rule: a content token in the key and a string value.
FAKE_VALIDATOR = """
import json, sys
from pathlib import Path
def _content_key(key, value):
    return "output" in key.split("_") and isinstance(value, str)
def walk(v, bad):
    if isinstance(v, dict):
        for k, i in v.items():
            if _content_key(k, i):
                bad.append(k)
            walk(i, bad)
if __name__ == "__main__":
    bad = []
    for p in Path(sys.argv[1], "evidence").rglob("*.json"):
        rec = json.loads(p.read_text())
        walk(rec, bad)
        if rec.get("readers", {}).get("cron", {}).get("result", {}).get("runs") == -1:
            bad.append("poison")
    print("failed" if bad else "passed")
    sys.exit(1 if bad else 0)
"""

FAKE_CRON = """
import json, sys
runs = -1 if "--poison" in open(sys.argv[sys.argv.index("--ledger") + 1]).read() else 7
print(json.dumps({"schema": "skrsi.baseline.cron.v1", "runs": runs,
                  "output_label": "x", "output_n": 3}))
"""


def load_module():
    spec = importlib.util.spec_from_file_location("skrsi_collect_instance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_instance(tmp_path: Path, readers: list[str], ledger_text: str = "ok") -> Path:
    inst = tmp_path / "inst"
    scripts = inst / "framework" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "cluster_config.py").write_text(textwrap.dedent(FAKE_CLUSTER_CONFIG))
    (scripts / "validate_instance.py").write_text(textwrap.dedent(FAKE_VALIDATOR))
    (scripts / "baseline_cron.py").write_text(textwrap.dedent(FAKE_CRON))
    cluster = {"nodes": [{"name": "node-a", "readers": readers}], "services": []}
    (inst / "cluster.yaml").write_text(json.dumps(cluster))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(ledger_text)
    return inst


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SKRSI_CRON_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("SKRSI_CARD_EVENTS", str(tmp_path / "absent"))
    return tmp_path


def run(mod, inst: Path) -> int:
    return mod.main(["--instance", str(inst), "--node", "node-a", "--date", DAY])


def test_writes_evidence_with_missingness_and_strips_content_like(env):
    mod = load_module()
    inst = make_instance(env, ["cron", "cardstore", "unknown"])
    assert run(mod, inst) == 0
    rec = json.loads((inst / "evidence" / "node-a" / f"{DAY}.json").read_text())
    assert rec["node"] == "node-a" and rec["date"] == DAY
    cron = rec["readers"]["cron"]
    assert cron["status"] == "ok" and cron["result"]["runs"] == 7
    assert "output_label" not in cron["result"] and cron["result"]["output_n"] == 3
    assert cron["dropped_fields"] == ["output_label"]
    assert rec["readers"]["cardstore"]["status"] == "missing"
    assert "result" not in rec["readers"]["cardstore"]
    assert rec["readers"]["unknown"]["status"] == "missing"


def test_existing_evidence_is_never_rewritten(env):
    mod = load_module()
    inst = make_instance(env, ["cron"])
    out = inst / "evidence" / "node-a" / f"{DAY}.json"
    out.parent.mkdir(parents=True)
    out.write_text('{"kept": true}\n')
    assert run(mod, inst) == 0
    assert out.read_text() == '{"kept": true}\n'


def test_undeclared_node_is_refused(env):
    mod = load_module()
    inst = make_instance(env, ["cron"])
    with pytest.raises(SystemExit):
        mod.main(["--instance", str(inst), "--node", "node-b", "--date", DAY])
    assert not (inst / "evidence").exists()


def test_failed_validation_removes_the_file(env):
    mod = load_module()
    inst = make_instance(env, ["cron"], ledger_text="--poison")
    assert run(mod, inst) == 1
    assert not (inst / "evidence" / "node-a" / f"{DAY}.json").exists()


def test_failing_reader_is_an_error_not_zero(env, monkeypatch):
    mod = load_module()
    inst = make_instance(env, ["cron"])
    (inst / "framework" / "scripts" / "baseline_cron.py").write_text(
        "import sys; sys.exit('boom')\n"
    )
    assert run(mod, inst) == 0
    cron = json.loads((inst / "evidence" / "node-a" / f"{DAY}.json").read_text())["readers"][
        "cron"
    ]
    assert cron == {"status": "error", "reason": "boom"}


# ---- resources reader -------------------------------------------------------

SHOW = {
    "web.service": {
        "LoadState": "loaded",
        "ActiveState": "active",
        "InvocationID": "inv1",
        "CPUUsageNSec": "50000000000",  # 50 s of CPU
        "ActiveEnterTimestampMonotonic": "1",  # 0 means never started
        "ControlGroup": "/app.slice/web.service",
    },
    "gone.service": {"LoadState": "not-found", "ActiveState": "inactive"},
    "idle.service": {"LoadState": "loaded", "ActiveState": "inactive"},
}


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


@pytest.fixture()
def res(monkeypatch, tmp_path):
    mod = load_module()
    cg = tmp_path / "cgroup" / "app.slice" / "web.service"
    (cg / "child").mkdir(parents=True)
    (cg / "cgroup.procs").write_text("11\n12\n")
    (cg / "child" / "cgroup.procs").write_text("13\n")
    (cg / "memory.current").write_text(str(300 * 1024 * 1024))
    (cg / "memory.peak").write_text(str(900 * 1024 * 1024))
    for pid, kb in (("11", 10240), ("12", 20480), ("13", 1024)):
        (tmp_path / "proc" / pid).mkdir(parents=True)
        (tmp_path / "proc" / pid / "status").write_text(f"Name:\tx\nVmRSS:\t   {kb} kB\n")
    monkeypatch.setattr(mod, "CGROUP_ROOT", tmp_path / "cgroup")
    monkeypatch.setattr(mod, "PROC_ROOT", tmp_path / "proc")
    monkeypatch.setenv("SKRSI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKRSI_RESOURCE_MOUNTS", str(tmp_path))
    monkeypatch.setenv("SKRSI_RESOURCE_UNITS", "web, gone system:idle")
    monkeypatch.delenv("SKRSI_SYNC_FOLDERS", raising=False)
    calls: list[list[str]] = []
    clock = {"us": 100_000_000}  # 100 s since boot
    journal = {"lines": "Main process exited, code=killed, status=9/KILL\n"}

    def fake_cmd(args):
        calls.append(args)
        if args[0] == "systemctl":
            unit = args[args.index("show") + 1]
            props = SHOW.get(unit, {"LoadState": "not-found"})
            return FakeProc("\n".join(f"{k}={v}" for k, v in props.items()))
        if "-n" in args:  # the journal access probe
            return FakeProc("" if journal["lines"] is None else "Started.\n")
        if journal["lines"] is None:
            return FakeProc("", 1)
        want = args[args.index("-g") + 1]
        return FakeProc(journal["lines"] if "killed" in want else "", 0 if "killed" in want else 1)

    monkeypatch.setattr(mod, "_cmd", fake_cmd)
    monkeypatch.setattr(mod, "_monotonic_us", lambda: clock["us"])
    return mod, calls, clock, journal, tmp_path


def test_resources_first_run_is_since_start_then_delta(res):
    mod, calls, clock, _, _ = res
    out = mod._resources(DAY)
    assert out["status"] == "ok"
    r = out["result"]
    assert r["units_declared"] == 3
    web = r["units"]["web"]
    # 50 s of CPU over 100 s wall = 50 percent of one core
    assert (web["cpu_core_pct"], web["cpu_basis"], web["cpu_window_s"]) == (
        50.0,
        "since_start",
        100,
    )
    assert web["rss_mb"] == 31.0  # 10 + 20 + 1 MB, child cgroup included
    assert (web["mem_current_mb"], web["mem_peak_mb"]) == (300.0, 900.0)
    assert (web["kills"], web["restarts"]) == (1, 0)
    assert r["units"]["gone"] == {"status": "missing", "reason": "unit not loaded"}
    assert r["units"]["system:idle"] == {"status": "missing", "reason": "unit not active"}
    assert ["systemctl", "show", "idle.service"] == calls[-1][:3] or any(
        c[:3] == ["systemctl", "show", "idle.service"] for c in calls
    )
    assert r["disk"]["mounts"] == 1 and 0 < r["disk"]["free_pct_min"] <= 100
    assert r["sync"] == {}
    # second run 100 s later, 10 s more CPU: a delta of 10 percent
    SHOW["web.service"]["CPUUsageNSec"] = "60000000000"
    clock["us"] += 100_000_000
    try:
        web = mod._resources(DAY)["result"]["units"]["web"]
    finally:
        SHOW["web.service"]["CPUUsageNSec"] = "50000000000"
    assert (web["cpu_core_pct"], web["cpu_basis"], web["cpu_window_s"]) == (10.0, "delta", 100)


def test_resources_restart_resets_to_since_start(res):
    mod, _, clock, _, _ = res
    mod._resources(DAY)
    SHOW["web.service"]["InvocationID"] = "inv2"
    clock["us"] += 100_000_000
    try:
        web = mod._resources(DAY)["result"]["units"]["web"]
    finally:
        SHOW["web.service"]["InvocationID"] = "inv1"
    assert web["cpu_basis"] == "since_start" and web["cpu_window_s"] == 200


def test_resources_unreadable_journal_is_null_not_zero(res):
    mod, _, _, journal, _ = res
    journal["lines"] = None
    web = mod._resources(DAY)["result"]["units"]["web"]
    assert web["kills"] is None and web["restarts"] is None


def test_resources_without_allowlist_is_missing(res, monkeypatch):
    mod = res[0]
    monkeypatch.delenv("SKRSI_RESOURCE_UNITS")
    assert mod._resources(DAY)["status"] == "missing"


def test_resources_sync_reads_local_api_and_never_emits_the_key(res, monkeypatch):
    mod, _, _, _, tmp = res
    cfg = tmp / "config.xml"
    cfg.write_text(
        '<configuration><gui tls="false"><address>0.0.0.0:8384</address>'
        "<apikey>SECRETKEY</apikey></gui></configuration>"
    )
    monkeypatch.setenv("SKRSI_SYNCTHING_CONFIG", str(cfg))
    monkeypatch.setenv("SKRSI_SYNC_FOLDERS", "fold-a,fold-b")
    seen = []

    def fake_http(url, key):
        seen.append((url, key))
        if "fold-a" in url:
            return {"globalFiles": 1000, "localFiles": 990, "needFiles": 10, "state": "idle"}
        return None

    monkeypatch.setattr(mod, "_http_json", fake_http)
    out = mod._resources(DAY)["result"]["sync"]
    assert out == {
        "fold-a": {"status": "ok", "files": 1000, "local_files": 990, "need_files": 10},
        "fold-b": {"status": "missing"},
    }
    assert seen[0] == ("http://127.0.0.1:8384/rest/db/status?folder=fold-a", "SECRETKEY")
    assert "SECRETKEY" not in json.dumps(out)


def test_resources_reader_lands_in_evidence(res, env):
    mod = res[0]
    inst = make_instance(env, ["resources"])
    assert run(mod, inst) == 0
    rec = json.loads((inst / "evidence" / "node-a" / f"{DAY}.json").read_text())
    assert rec["readers"]["resources"]["status"] == "ok"
    assert rec["readers"]["resources"]["result"]["units"]["web"]["status"] == "ok"


def test_node_name_comes_from_env_before_hostname(env, monkeypatch):
    mod = load_module()
    inst = make_instance(env, ["cron"])
    monkeypatch.setenv("SKRSI_NODE", "node-a")
    assert mod.main(["--instance", str(inst), "--date", DAY]) == 0
    assert (inst / "evidence" / "node-a" / f"{DAY}.json").exists()
