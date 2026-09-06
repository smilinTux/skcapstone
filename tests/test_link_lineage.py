from pathlib import Path
import json
import importlib.util

_spec = importlib.util.spec_from_file_location("link_lineage", Path(__file__).parents[1] / "scripts/fleet/link-lineage.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
reconcile = _mod.reconcile


def card(cid, title):
    return {"id": cid, "title": title, "description": "", "acceptance_criteria": []}


def test_duplicate_missing_review_and_exclusion(tmp_path):
    prs = [{"number": 1, "headRefOid": "a"}, {"number": 2}, {"number": 3}, {"number": 4}]
    cards = [card("11111111", "source PR 1"), card("22222222", "source PR 1"), card("33333333", "source PR 2"), card("44444444", "source PR 3"), card("55555555", "review PR 3")]
    for cid in {"11111111", "22222222", "33333333", "44444444", "55555555"}:
        p = tmp_path / "cards" / cid / "events"; p.mkdir(parents=True)
        (p / "e.jsonl").write_text(json.dumps({"event_id": "rev-" + cid, "action": "evidence"}) + "\n")
    report = reconcile(prs, cards, tmp_path, {"4": "stale unmanaged PR"})
    assert report["coverage"] == {"lineage-complete": 1, "excluded": 1, "unresolved": 2}
    assert report["unresolved_prs"] == [1, 2]
    complete = next(x for x in report["records"] if x["pr"] == 3)
    assert complete["source_generation"] == "rev-44444444"
    assert complete["review_revision"] == "rev-55555555"


def test_report_is_deterministic_and_does_not_touch_feed(tmp_path):
    report = reconcile([{"number": 7}], [], tmp_path)
    assert report["coverage"]["unresolved"] == 1
    assert not (tmp_path / "observations.jsonl").exists()
