"""SKRSI fleet-heartbeat collector: acceptance, idempotency, and data boundary."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "skrsi" / "collect_fleet.py"
NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)


def load_module():
    spec = importlib.util.spec_from_file_location("skrsi_collect_fleet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_home(tmp_path: Path, nodes: dict[str, str | None]) -> Path:
    home = tmp_path / "skcapstone"
    for node, reported in nodes.items():
        d = home / "fleet" / "status" / node
        d.mkdir(parents=True, exist_ok=True)
        body = {"spec": {"spec": {"role": "control"}}}
        if reported is not None:
            body["reportedAt"] = reported
        (d / "node.json").write_text(json.dumps(body), encoding="utf-8")
    return home


def test_every_node_yields_one_accepted_observation(tmp_path):
    cf = load_module()
    home = make_home(
        tmp_path, {"node-a": "2026-09-11T11:59:00Z", "node-b": "2026-09-11T11:58:00Z"}
    )
    envelopes = cf.node_observations(home, NOW)
    assert len(envelopes) == 2
    # Cursors must strictly increase: the collector rejects a non-advancing one.
    cursors = [e["cursor"] for e in envelopes]
    assert cursors == sorted(cursors) and len(set(cursors)) == 2


def test_age_is_measured_not_guessed(tmp_path):
    cf = load_module()
    home = make_home(tmp_path, {"node-a": "2026-09-11T11:59:00Z"})
    (envelope,) = cf.node_observations(home, NOW)
    assert envelope["metadata"]["duration_ms"] == pytest.approx(60_000.0)


def test_unparsable_report_is_partial_not_zero(tmp_path):
    """Missing data must stay explicit; a fabricated 0ms age would read as healthy."""
    cf = load_module()
    home = make_home(tmp_path, {"node-a": None})
    (envelope,) = cf.node_observations(home, NOW)
    assert envelope["quality"] == "partial"
    assert "duration_ms" not in envelope["metadata"]


def test_same_instant_dedupes_without_growing_the_outbox(tmp_path):
    """The fence key and the fenced bytes share resolution, so a retry is a duplicate.

    Before quantizing, natural_key used whole seconds while occurred_at carried
    microseconds, so two runs in one second produced the same key with different
    bytes and the collector correctly rejected them as a natural key conflict.
    """
    cf = load_module()
    home = make_home(tmp_path, {"node-a": "2026-09-11T11:59:00Z"})
    first = cf.node_observations(home, NOW)
    again = cf.node_observations(home, NOW.replace(microsecond=987654))
    assert first == again, "same second must produce byte-identical envelopes"


def test_later_instant_is_a_new_observation(tmp_path):
    cf = load_module()
    home = make_home(tmp_path, {"node-a": "2026-09-11T11:59:00Z"})
    first = cf.node_observations(home, NOW)
    later = cf.node_observations(home, NOW + timedelta(seconds=30))
    assert first[0]["natural_key"] != later[0]["natural_key"]
    assert later[0]["metadata"]["duration_ms"] > first[0]["metadata"]["duration_ms"]


def test_persisted_record_hashes_the_node_name(tmp_path):
    """Data boundary: strings become correlation handles, numbers stay readable."""
    cf = load_module()
    from skcapstone.skrsi_collector import BoundedCollector
    from skcapstone.skrsi_registry import AppendOnlyOutbox

    home = make_home(tmp_path, {"node-secret-name": "2026-09-11T11:59:00Z"})
    outbox = AppendOnlyOutbox(tmp_path / "outbox.jsonl")
    collector = BoundedCollector(
        outbox,
        source=cf.SOURCE,
        target_ref=cf.TARGET_REF,
        authority="SKFleet",
        handoff_owner="niobe",
    )
    for envelope in cf.node_observations(home, NOW):
        collector.submit(envelope)
    result = collector.drain(now=NOW)
    assert result.accepted == 1 and result.rejected == 0

    raw = (tmp_path / "outbox.jsonl").read_text(encoding="utf-8")
    assert "node-secret-name" not in raw, "clear node name must not be persisted"
    record = json.loads(outbox.entries()[0].serialized)
    metadata = record["payload"]["metadata"]
    assert metadata["host"].startswith("sha256:")
    assert metadata["duration_ms"] == pytest.approx(60_000.0)


def test_state_dir_inside_home_is_refused(tmp_path):
    """SKRSI state must not live in the Syncthing-synced tree."""
    cf = load_module()
    home = make_home(tmp_path, {"node-a": "2026-09-11T11:59:00Z"})
    with pytest.raises(SystemExit):
        cf.main(["--home", str(home), "--state-dir", str(home / "skrsi")])
