"""Durable crew state stays local, bounded and replay-safe."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from skcapstone.fleet.crew_store import CrewStore


def manifest():
    return {"packet": {"crew_id": "test-crew"}, "manifest_sha256": "a" * 64}


def request():
    return {
        "crew_id": "test-crew",
        "request_id": "request-1",
        "slot_id": "lookup",
        "packet": {"reason": "exact bytes"},
        "packet_sha256": "b" * 64,
        "dedup_key": "c" * 64,
        "canonical_request_id": "request-1",
        "state": "requested",
    }


def test_manifest_exact_replay_and_conflict(paths):
    store = CrewStore(paths, node="fixture-host")
    with store.lock():
        assert store.put_manifest(manifest()) == manifest()
        assert store.put_manifest(manifest()) == manifest()
        bad = manifest()
        bad["manifest_sha256"] = "d" * 64
        with pytest.raises(ValueError, match="conflict"):
            store.put_manifest(bad)
    with CrewStore(paths, node="fixture-host").lock() as reopened:
        assert reopened.list_manifests() == [manifest()]


def test_request_progress_does_not_rewrite_custody(paths):
    store = CrewStore(paths, node="fixture-host")
    with store.lock():
        row = store.save_request(request())
        row.update(state="creating", helper_packet={"request_id": "helper-1"})
        store.save_request(row)
        row.update(state="assigned", helper_id="aaaa1111")
        store.save_request(row)
        changed = copy.deepcopy(row)
        changed["helper_id"] = "bbbb2222"
        with pytest.raises(ValueError, match="immutable"):
            store.save_request(changed)
        changed = copy.deepcopy(row)
        changed["packet"]["reason"] = "changed"
        with pytest.raises(ValueError, match="immutable"):
            store.save_request(changed)
        assert store.get_request("test-crew", "request-1") == row


def test_parallel_exact_replay_has_one_file(paths):
    def put(_):
        with CrewStore(paths, node="fixture-host").lock() as store:
            return store.put_manifest(manifest())

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(put, range(8))) == [manifest()] * 8
    with CrewStore(paths, node="fixture-host").lock() as store:
        assert len(store.list_manifests()) == 1


def test_store_rejects_symlink_and_invalid_ids(paths, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.root.mkdir()
    (paths.root / "crews").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        with CrewStore(paths, node="fixture-host").lock():
            pass
    with pytest.raises(ValueError):
        CrewStore(paths, node="../host")


def test_corrupt_or_duplicate_keys_are_not_treated_as_empty(paths):
    store = CrewStore(paths, node="fixture-host")
    with store.lock():
        store.put_manifest(manifest())
        file = next(store.directory.glob("manifest-*.json"))
        file.write_text('{"packet": {}, "packet": {}}')
        with pytest.raises(ValueError):
            store.get_manifest("test-crew")
        file.write_text(json.dumps([1]))
        with pytest.raises(ValueError):
            store.get_manifest("test-crew")


def test_size_bound_and_missing_lock(paths):
    store = CrewStore(paths, node="fixture-host")
    assert store.list_manifests() == []
    assert store.list_requests() == []
    assert not paths.root.exists()
    with pytest.raises(ValueError, match="lock"):
        store.put_manifest(manifest())
    with store.lock():
        row = manifest()
        row["extra"] = "x" * 262144
        with pytest.raises(ValueError, match="bound"):
            store.put_manifest(row)


def test_tolerant_scan_preserves_healthy_crew(paths):
    store = CrewStore(paths, node="fixture-host")
    with store.lock():
        store.put_manifest(manifest())
        (store.directory / "manifest-broken.json").write_text('{"packet": 1}')
        (store.directory / "manifest-wrong-shape.json").write_text("[1]")
    records, errors = store.scan_manifests()
    assert records == [manifest()]
    assert {row["crew_id"] for row in errors} == {"broken", "wrong-shape"}
    with pytest.raises(ValueError):
        store.list_manifests()


def test_corrupt_request_holds_only_its_crew(paths):
    store = CrewStore(paths, node="fixture-host")
    with store.lock():
        row_a = request()
        row_b = request()
        row_b["crew_id"] = "healthy-crew"
        store.save_request(row_a)
        store.save_request(row_b)
        broken = store.directory / store._request_name(row_a["crew_id"], row_a["request_id"])
        broken.write_text("{")
        assert store.list_requests("healthy-crew") == [row_b]
        with pytest.raises(ValueError):
            store.list_requests("test-crew")
        with pytest.raises(ValueError):
            store.list_requests()
