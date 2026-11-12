"""The read-only claim-expiry report, driven against a REAL CardStore.

`observe()` reads the CardStore fold rather than replaying raw event files,
so these fixtures build cards through the store API. An earlier version of
this file wrote synthetic JSONL directly, which meant it asserted on a
contract the code no longer has.
"""

from __future__ import annotations

import itertools
import json
import time
from datetime import datetime, timezone

from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import claim_expiry_cli

_COUNTER = itertools.count(1)


def _iso_at(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, timezone.utc).isoformat()


def _claimed(home, *, owner, idle_hours, revision="rev-1"):
    """A card claimed by `owner` whose last owner event is `idle_hours` old.

    The claim goes through the store so the fold sees it, then its `ts` is
    rewritten on disk to age it. Backdating is the only way to produce an
    old claim without waiting, and it exercises exactly the field that
    `observe()` reads for idleness.
    """
    store = CardStore(home)
    card_id = f"{next(_COUNTER):08x}"
    store.create(CardCore(id=card_id, title=f"card {card_id}"))
    store.append_event(card_id, "claim", owner, owner=owner, claim_revision=revision)

    stamp = _iso_at(time.time() - idle_hours * 3600.0)
    for shard in (home / "cards" / card_id / "events").glob("*.jsonl"):
        out = []
        for line in shard.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("writer") == owner or event.get("owner") == owner:
                event["ts"] = stamp
            out.append(json.dumps(event))
        shard.write_text("\n".join(out) + "\n", encoding="utf-8")
    return card_id


def test_reports_reclaimable_as_json(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SKFLEET_CLAIM_TTL_MODE", raising=False)
    stale = _claimed(tmp_path, owner="jarvis", idle_hours=100)
    fresh = _claimed(tmp_path, owner="live-worker", idle_hours=1)

    assert claim_expiry_cli.main(["--home", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {r["card_id"]: r for r in payload["verdicts"]}

    assert rows[stale]["reclaimable"] is True
    assert rows[stale]["reason"] == "idle-beyond-ttl"
    assert rows[fresh]["reclaimable"] is False
    assert rows[fresh]["reason"] == "within-ttl"
    assert payload["mode"] == "off"
    assert payload["ttl_hours"] == 48.0
    assert payload["held_count"] == 2
    assert payload["reclaimable_count"] == 1


def test_exit_code_is_zero_even_with_findings(tmp_path):
    """A report is not a failure. Nothing should go red because the fleet
    has expired claims."""
    _claimed(tmp_path, owner="jarvis", idle_hours=500)
    assert claim_expiry_cli.main(["--home", str(tmp_path), "--json"]) == 0
    assert claim_expiry_cli.main(["--home", str(tmp_path)]) == 0


def test_exit_code_is_zero_on_an_empty_store(tmp_path):
    assert claim_expiry_cli.main(["--home", str(tmp_path), "--json"]) == 0


def test_reclaimable_only_filters_output(tmp_path, capsys):
    stale = _claimed(tmp_path, owner="jarvis", idle_hours=100)
    fresh = _claimed(tmp_path, owner="live-worker", idle_hours=1)

    claim_expiry_cli.main(["--home", str(tmp_path), "--json", "--reclaimable-only"])
    payload = json.loads(capsys.readouterr().out)
    shown = {r["card_id"] for r in payload["verdicts"]}

    assert shown == {stale}
    assert fresh not in shown
    # The counts still describe the WHOLE store, not the filtered view, or a
    # reader cannot tell a quiet fleet from a filtered report.
    assert payload["held_count"] == 2
    assert payload["reclaimable_count"] == 1


def test_text_output_is_not_json(tmp_path, capsys):
    stale = _claimed(tmp_path, owner="jarvis", idle_hours=100)
    claim_expiry_cli.main(["--home", str(tmp_path)])
    out = capsys.readouterr().out

    assert stale in out
    assert "RECLAIM" in out
    assert "jarvis" in out
    assert "ttl=48.0h" in out
    try:
        json.loads(out)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("text mode must not emit JSON")


def test_never_writes_anything_under_home(tmp_path):
    """The report is read-only, and it runs on a live production store."""
    _claimed(tmp_path, owner="jarvis", idle_hours=100)

    def snapshot():
        return {p: p.stat().st_mtime_ns for p in sorted(tmp_path.rglob("*")) if p.is_file()}

    before = snapshot()
    claim_expiry_cli.main(["--home", str(tmp_path), "--json"])
    claim_expiry_cli.main(["--home", str(tmp_path)])
    after = snapshot()

    assert after == before, "the report mutated the store"


def test_reports_mode_from_env(tmp_path, capsys, monkeypatch):
    for value, expected in (
        ("report", "report"),
        ("enforce", "enforce"),
        ("banana", "off"),
        ("", "off"),
    ):
        monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", value)
        claim_expiry_cli.main(["--home", str(tmp_path), "--json"])
        assert json.loads(capsys.readouterr().out)["mode"] == expected, value


def test_a_misconfigured_ttl_does_not_mark_a_fresh_claim_reclaimable(
    tmp_path, capsys, monkeypatch
):
    """End to end guard on the nan hazard: left through, it made every claim
    in the store reclaimable, including one made a minute ago."""
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_H", "nan")
    _claimed(tmp_path, owner="live-worker", idle_hours=0.02)
    claim_expiry_cli.main(["--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ttl_hours"] == 48.0
    assert payload["reclaimable_count"] == 0
