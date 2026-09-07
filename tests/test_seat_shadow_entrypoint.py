"""Tests for the non-mutating Niobe shadow beat."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.seat_shadow_entrypoint import run_shadow


def test_niobe_shadow_records_presence_and_no_mutation(tmp_path: Path, monkeypatch) -> None:
    control = tmp_path / "control.json"
    control.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {"link": ["chiap08"], "mero": ["chiap08"], "niobe": ["chiap08"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("skcapstone.seat_shadow_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr(
        "skcapstone.seat_shadow_entrypoint.poll_mail",
        lambda _seat: type("Poll", (), {"as_dict": lambda self: {"mailbox_ok": True}})(),
    )
    record = run_shadow(home=tmp_path / "home", control_plane=control, local_host="chiap08")
    assert record["seat"] == "niobe"
    assert record["activation_state"] == "shadow_only"
    assert record["mutation"] is False
    assert (tmp_path / "home/coordination/seat-cycles/niobe.shadow.jsonl").exists()
