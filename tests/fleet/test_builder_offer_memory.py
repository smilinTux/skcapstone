from skcapstone.fleet import builder_offer_memory as m


def test_dead_outcomes_are_skipped_then_retried(tmp_path):
    path = tmp_path / "memory.json"
    m.record(path, "dead0001", "ineligible:qualified Node environment changed", now=1000)
    memory = m.load(path)
    assert m.skip(memory, "dead0001", now=1000 + 60)
    assert not m.skip(memory, "dead0001", now=1000 + m.DEAD_SECONDS + 1)


def test_order_serves_qualified_then_least_recently_tried():
    memory = {
        "old00001": {"ts": 100, "state": "pending:chiap01"},
        "new00002": {"ts": 900, "state": "pending:chiap02"},
        "dead0003": {"ts": 950, "state": "deferred:card-not-ready-or-owned"},
    }
    pool = [
        (0, 0, cid, {}, []) for cid in ("new00002", "dead0003", "old00001", "never004", "qual0005")
    ]
    ordered = m.order(pool, memory, lambda cid: cid == "qual0005", now=1000)
    assert [c[2] for c in ordered] == ["qual0005", "never004", "old00001", "new00002"]


def test_record_is_bounded(tmp_path):
    path = tmp_path / "memory.json"
    m.record(path, "stale001", "pending:x", now=0)
    m.record(path, "fresh002", "pending:y", now=m.KEEP_SECONDS + 10)
    assert set(m.load(path)) == {"fresh002"}
