"""Per-lane BLOCKED verdict telemetry tests."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet_verdict_stats.py"
SPEC = importlib.util.spec_from_file_location("skfleet_verdict_stats", MODULE_PATH)
stats = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(stats)


def _append(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")


def test_counts_blocked_by_lane_and_reason_without_inferred_verdicts():
    messages = [
        {"from": "pi-qwen-host-a", "body": "BLOCKED blocked_on=dependency card:123"},
        {"from": "pi-glm-host-b", "body": "BLOCKED: capability limit"},
        {"from": "pi-kimi-host-c", "body": "BLOCKED after context window exhausted"},
        {"from": "pi-codex-host-d", "body": "BLOCKED: credentials unavailable"},
        {"from": "pi-esc-host-e", "body": "BLOCKED because tests failed"},
        {"lane": "codex", "from": "worker", "body": "BLOCKED with unexplained issue"},
        {"lane": "qwen", "from": "worker", "body": "PASS but dependency remains linked"},
        {"lane": "glm", "from": "worker", "body": "lifecycle status blocked"},
    ]

    counts = stats.count_verdicts(messages)

    assert counts["qwen"]["dependency"] == 1
    assert counts["glm"]["capability"] == 1
    assert counts["kimi"]["context"] == 1
    assert counts["codex"]["credentials"] == 1
    assert counts["escalate"]["test-failure"] == 1
    assert counts["codex"]["other"] == 1
    assert sum(counts["qwen"].values()) == 1
    assert sum(counts["glm"].values()) == 1


def test_first_scan_does_not_backfill_and_incremental_scan_writes_json(tmp_path):
    mail = tmp_path / "mail"
    evidence = tmp_path / "evidence"
    mail.mkdir()
    mailbox = mail / "worker@host.jsonl"
    _append(mailbox, {"lane": "qwen", "body": "BLOCKED dependency old"})
    today = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)

    assert stats.update(mail, evidence, today) is None
    first = json.loads((evidence / "2026-09-03.json").read_text())
    assert sum(sum(reasons.values()) for reasons in first["counts"].values()) == 0

    _append(mailbox, {"lane": "qwen", "body": "BLOCKED dependency card:abc"})
    _append(mailbox, {"lane": "codex", "body": "PASS sha256:abc"})
    assert stats.update(mail, evidence, today) is None

    artifact = json.loads((evidence / "2026-09-03.json").read_text())
    assert artifact == {"date": "2026-09-03", "counts": {
        lane: {reason: (1 if lane == "qwen" and reason == "dependency" else 0)
               for reason in stats.REASONS}
        for lane in stats.LANES
    }}


def test_new_mailbox_after_baseline_is_counted(tmp_path):
    mail = tmp_path / "mail"
    evidence = tmp_path / "evidence"
    mail.mkdir()
    today = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    stats.update(mail, evidence, today)
    _append(mail / "new-worker@host.jsonl", {"lane": "glm", "body": "BLOCKED context limit"})

    stats.update(mail, evidence, today)

    artifact = json.loads((evidence / "2026-09-03.json").read_text())
    assert artifact["counts"]["glm"]["context"] == 1


def test_emits_previous_daily_summary_once(tmp_path):
    mail = tmp_path / "mail"
    evidence = tmp_path / "evidence"
    mail.mkdir()
    mailbox = mail / "worker@host.jsonl"
    mailbox.touch()
    day_one = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    day_two = datetime(2026, 9, 4, 1, tzinfo=timezone.utc)
    stats.update(mail, evidence, day_one)
    _append(mailbox, {"from": "pi-kimi-host-card", "body": "BLOCKED: test-failure"})
    stats.update(mail, evidence, day_one)

    summary = stats.update(mail, evidence, day_two)

    assert summary is not None
    assert summary.startswith("VERDICT_STATS|date=2026-09-03|counts=")
    assert '"kimi":{"capability":0,"context":0,"credentials":0,"dependency":0,"other":0,"test-failure":1}' in summary
    assert stats.update(mail, evidence, day_two) is None
