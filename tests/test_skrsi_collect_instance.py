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
