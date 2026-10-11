# Completion evidence — Card ff091db4

**Card:** `[M] Index fleet launch history incrementally`
**Owner:** pi-glm-chiap08-ff091db4
**Claim revision:** 2e72ddc25882431489016d3096f6ad1d
**Authorized base:** 10a9da3ac853cdeb677e9c57bb20550b33088869
**Repository:** https://github.com/smilinTux/skcapstone (PR #969)

## What this delivers

The rotation launch-history scan (49,000 `evidence/fleet-rotation/*/actions.log`
files, 923 MiB on chiap08) previously read every historical log byte on every
cycle, costing >2 min CPU and ~1.1 GiB RSS before selection. This change adds a
host-local, atomic, append-aware cache keyed by per-file metadata, while
preserving the exact retry / material-change / strong-lane semantics of the
previous full scan.

### Files changed

- `scripts/fleet/skfleet-rotate.py` — the full `glob(actions.log)` scan is
  replaced by four helpers:
  - `_scan_launch_history_log` — parses one log into summary rows
    (card, wake-classified, strong-classified) with identical line semantics to
    the previous inline parser (`LAUNCHED|` prefix, `len==4+`, 8-field
    `lane/model/owner/claim_revision` wake shape, `model=<strong>` membership).
  - `_aggregate_launch_history` — builds `_launched_at`,
    `_wake_launch_times`, `_strong_launched_at` with the same
    `max(epoch)` / append-per-wake-launch semantics as before.
  - `_scan_launch_history_full` — cache-free rebuild (used on cache OSError and
    by tests), scanning only regular files, no symlink following.
  - `_load_launch_history` — lock-file guarded (`flock`, 0600, owner-checked,
    `st_nlink==1`, no symlink follow), re-reads only logs whose
    `(st_ino, st_size, st_mtime_ns, st_ctime_ns)` signature changed, skips
    reparse when the signature is unchanged, drops removed entries, validates
    the cached JSON (schema_version, evidence_root, strong_model, per-record
    shape and 64 MiB size bound) and rebuilds on any invalidity, writes the
    cache atomically (`O_EXCL` temp + `fsync` + `os.replace`).
  - The cache lives at `~/.cache/skcapstone/fleet/launch-history-v1.json`; any
    `OSError` from the cache path logs `LAUNCH_HISTORY_CACHE_BYPASS` and falls
    back to the full scan, so a bad cache can never break a cycle.
  - `_launched` (populated but unused, per the TDD) is removed; the stale
    comment referencing it at the `unclaimable()` retry check is removed.
  - Downstream semantics untouched: `_wake_launch_times` retries vs generation
    (line ~4520), `_strong_launched_at` strong lane (~4709), and
    `_material_change_since`/`_launched_at` (~4771) consume the same maps.
  - No cycle timeout was raised.
- `tests/test_skfleet_launch_history_cache.py` — new focused tests (below).
- `changelog.d/ff091db4-launch-history-cache.md` — changelog fragment.

## Tests (card's required selection, run once each)

```
$ python -m pytest -q tests/test_skfleet_launch_history_cache.py tests/test_skfleet_backoff_wake.py tests/test_skfleet_provisional_opener.py
87 passed in 5.22s
```

Coverage per the TDD:

- **cold build** — `test_cold_build_aggregates_launch_timestamps_and_classifications`:
  two cycle logs, one legacy row; asserts exact `_launched_at`,
  `_wake_launch_times`, `_strong_launched_at` maps including the
  wake-classified legacy row and strong-model reclassification.
- **unchanged fast read** — `test_unchanged_logs_are_not_reparsed`: second
  load with identical metadata never calls the parser.
- **appended launch** — `test_appended_launch_updates_only_changed_file`: only
  the appended log is reparsed; wake retry count for the appended card
  becomes 2.
- **replaced/truncated log** — `test_truncated_or_removed_log_replaces_its_cached_rows`:
  truncation replaces cached rows; `replace()` with a new inode (ctime/ino
  change) reparses; deletion drops the entry.
- **invalid cache** — `test_malformed_cache_rebuilds_from_source_logs`.
- **strong-lane reclassification** — `test_strong_model_change_reclassifies_cached_history`.

Guarded regression suites that pin the retry/wake and provisional-opener
semantics this cache must preserve: `tests/test_skfleet_backoff_wake.py` (35
tests) and `tests/test_skfleet_provisional_opener.py` (31 tests), all green.

```
$ python -m py_compile scripts/fleet/skfleet-rotate.py
(no output; exit 0)
$ git diff --check
(no output; exit 0)
```

## Verification notes and limitations

- Tests extract the helpers via `ast` from the rotator script without
  executing the module (the script performs live fleet actions at import
  time); this is the established pattern in the neighboring suites.
- The signature key includes `st_ctime_ns`/`st_ino`, so a log replaced via
  rename (new inode) or rewritten in place is always reparsed; a same-inode
  append changes `st_size`, also reparsed. Unchanged metadata means unchanged
  bytes for append-only writers, which is the fleet writer's behavior.
- Cache writes are host-local under `~/.cache`; no cross-host sync is assumed.
- No cycle timeouts were changed; per the card's constraint, latency is
  addressed by the cache alone.
- The commit/branch/PR identity for this candidate is recorded on the card via
  `coord link` (commit_sha, branch) rather than embedded here.
