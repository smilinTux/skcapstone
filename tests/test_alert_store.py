"""Tests for the durable alert record store (P4, card c6a87139)."""

from __future__ import annotations

from skcapstone import alert_store


def test_raise_alert_persists_a_record(tmp_path):
    record = alert_store.raise_alert(
        tmp_path,
        "gmktec-rma-1",
        "GMKtec warranty RMA follow-up",
        description="vendor email condensed",
        options=[{"text": "send the escalation email", "mode": "dry-run"}],
    )
    assert record["id"] == "gmktec-rma-1"
    assert record["title"] == "GMKtec warranty RMA follow-up"
    assert record["options"] == [{"text": "send the escalation email", "mode": "dry-run"}]
    assert record["created_at"]


def test_get_alert_reads_back_what_was_raised(tmp_path):
    alert_store.raise_alert(tmp_path, "a1", "title", description="d", options=["snooze"])
    got = alert_store.get_alert(tmp_path, "a1")
    assert got is not None
    assert got["title"] == "title"
    assert got["options"] == ["snooze"]


def test_get_alert_unknown_id_is_none(tmp_path):
    assert alert_store.get_alert(tmp_path, "nope") is None


def test_get_alert_corrupt_record_is_none(tmp_path):
    d = tmp_path / "coordination" / "alerts"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    assert alert_store.get_alert(tmp_path, "bad") is None


def test_raise_alert_is_idempotent_create_or_skip(tmp_path):
    first = alert_store.raise_alert(tmp_path, "a1", "first title", options=["opt1"])
    second = alert_store.raise_alert(tmp_path, "a1", "second title (should be ignored)")
    assert second["title"] == "first title"
    assert second["id"] == first["id"]


def test_list_alerts_sorted_by_created_at(tmp_path):
    alert_store.raise_alert(tmp_path, "a1", "one", created_at="2026-08-01T00:00:00+00:00")
    alert_store.raise_alert(tmp_path, "a2", "two", created_at="2026-08-02T00:00:00+00:00")
    out = alert_store.list_alerts(tmp_path)
    assert [r["id"] for r in out] == ["a1", "a2"]


def test_list_alerts_empty_store_is_empty_list(tmp_path):
    assert alert_store.list_alerts(tmp_path) == []


def test_list_alerts_skips_corrupt_record(tmp_path):
    alert_store.raise_alert(tmp_path, "good", "ok")
    d = tmp_path / "coordination" / "alerts"
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    out = alert_store.list_alerts(tmp_path)
    assert [r["id"] for r in out] == ["good"]


def test_raise_alert_default_priority_and_labels(tmp_path):
    record = alert_store.raise_alert(tmp_path, "a1", "t")
    assert record["priority"] == "high"
    assert record["labels"] == []
    assert record["created_by"] == "alert"
