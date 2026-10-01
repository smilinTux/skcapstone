# a31d4e54 — Fiber refill progress beyond running jobs and inbox pages

Status: local source candidate for independent cross-machine review. No
deployment, no hosted CI, no native card completion, no whole-fleet trial and
no acceptance is asserted by this document. The controller records execution,
not acceptance.

Card: `a31d4e54`. Owner: `pi-fiber-implementation-a31d4e54`.
Claim revision: `ddb6282b27a74bb6aba664858f23b6c9` (matches the controller
snapshot `/run/fleet/card.json`).
Workspace source revision at start: `5f621b47bfe98efe4bdee59282cb06ba5c20bf7b`
(matches `base_revision`; `git rev-parse HEAD` at start equalled it).
Dependency: `cfa0416c` (skcapstone test plane).

## Changed files (exact scope)

| File | Change |
|------|--------|
| `ops/fiber-f1be0929/fiber_refill.py` | Unlaunched-only buffer accounting; removed fatal directory-entry ceiling; metadata-only name enumeration + bounded lexical cursor pages; skip recorded-run names before decode; atomic persisted wrap-around cursor; HOLD/dry-run/safe-cursor invariants; byte-preserving terminal archival. |
| `ops/fiber-f1be0929/fiber_selection.py` | Removed global qualified-inbox depth accounting; adds at most `MAX_SELECT=2` NEW pinned qualifications per pass; existing destinations and recorded-run cards untouched; bounded policy shape/expiry/promoter/native gates preserved. |
| `ops/fiber-f1be0929/fiber_control.py` | Added `queued_jobs` bounded fair dispatch page: skips recorded runs before JSON decode, persisted cursor, `_read` no-follow bounded reader; called under the existing authority lock. Downstream verifier/dispatcher unchanged. |
| `ops/fiber-f1be0929/tests/test_fiber_refill.py` | 19 → 27 methods: updated obsolete overfull-inbox assertion to the progress contract while retaining the unsafe-directory refusal; added running/launching buffer, >32 recorded entries, cursor progress/wrap, dry-run cursor, unsafe/interrupted cursor and unlaunched-buffer regressions. |
| `ops/fiber-f1be0929/tests/test_fiber_selection.py` | 21 → 24 methods: replaced the obsolete two-receipt global-limit test with bounded-new-per-pass/no-republish assertions and large-unrelated-history coverage. |
| `ops/fiber-f1be0929/tests/test_fiber_control.py` | 27 → 28 methods: added bounded fair queue page coverage (40 recorded-run names skipped before decode; one 32-name page decoded). |
| `changelog.d/a31d4e54-refill-progress.md` | New changelog fragment (CHANGELOG.md not edited). |
| `docs/evidence/agents/a31d4e54/COMPLETION-EVIDENCE.md` | This document. |

## Acceptance mapping

1. **Unlaunched-only capacity; retained states never replayable.** `refill_queue`
   computes `unlaunched = [name for name in queued_names if name[:-5] not in runs]`.
   RUNNING/LAUNCHING/TERMINAL and other retained run states do not consume the
   two-item buffer and are not decoded for re-admission. Terminal entries are
   archived byte-preserving; all other retained queue files are left exact.
   Tests: `test_running_and_launching_entries_do_not_consume_buffer`,
   `test_two_genuinely_unlaunched_entries_still_fill_the_buffer`,
   `test_running_requests_remain_untouched`,
   `test_terminal_archive_crash_retains_original_and_recovers_idempotently`.
2. **No fatal directory-entry ceiling.** `_paths` (which raised
   `refill-too-many-inputs` above 32 entries) is replaced by `_names`, a
   metadata-only flat `*.json` enumeration. Directory size is never a
   capacity decision. `MAX_BYTES`, private regular-file, no-follow, ownership,
   source and qualification checks are preserved. Tests:
   `test_overfull_inbox_progresses_and_unsafe_directory_refuses`,
   `test_more_than_32_recorded_entries_do_not_block_and_are_not_decoded`,
   `test_symlink_world_writable_oversized_and_extra_fields_refused`.
3. **Skip recorded runs before decode; bounded page memory.** Queue and
   qualification pages filter `name[:-5] not in runs` before decoding, then
   `_page` returns at most `MAX_INPUTS` (32) names. Native `read_cards` is
   called only for decoded page entries. Tests:
   `test_more_than_32_recorded_entries_do_not_block_and_are_not_decoded`
   (asserts `read_cards` called once with exactly the one eligible card),
   `test_controller_decodes_only_a_bounded_fair_queue_page`.
4. **Eventual progress via persisted lexical cursor.** `_read_cursor`,
   `_write_cursor` and `_page` implement a per-scan (`queue`/`qualified`, plus
   `dispatch` for the controller) lexical cursor that advances and wraps. It is
   published atomically in the private state directory with `os.replace`. A
   missing cursor starts scanning; unsafe inputs are refused; HOLD precedes all
   reads/writes; dry runs write nothing. Tests:
   `test_cursor_advances_past_a_page_of_bad_names_across_fresh_invocations`,
   `test_cursor_wraps_and_is_deterministic_on_repeat`,
   `test_dry_run_does_not_write_a_cursor`,
   `test_unsafe_or_interrupted_cursor_inputs_are_rejected`,
   `test_interrupted_cursor_publication_never_replays_work`,
   `test_hold_precedes_all_reads_and_writes`.
5. **Selection global-depth accounting removed.** `select_requests` no longer
   enumerates or decodes the historical qualified inbox. It adds at most two
   NEW pinned cards per pass (`MAX_SELECT`), skips existing destinations and
   recorded-run cards, and preserves expiry, promoter, label-scope and native
   gates. Tests:
   `test_selection_adds_at_most_two_new_and_never_republishes`,
   `test_selection_ignores_large_unrelated_qualification_history`,
   `test_own_existing_destination_is_never_republished`,
   `test_existing_destinations_are_not_republished_but_do_not_limit_new`,
   `test_uses_existing_promoter_holds_and_no_action_rules`,
   `test_prior_runs_never_replayed`.
6. **Bounded controller dispatch page under the authority lock.** `cycle` calls
   `queued_jobs(QUEUE, STATE)` inside `with authority(STATE)`; `queued_jobs`
   reads `_runs`, skips recorded runs before `_read`, and returns at most one
   `MAX_INPUTS` cursor page. `dispatch_one`, `verify_source`, `eligible`,
   `claim_for_worker` and reconciliation are unchanged. Tests:
   `test_controller_decodes_only_a_bounded_fair_queue_page`,
   `test_empty_queue_does_not_start_or_claim`,
   `test_controller_checks_selection_inside_authority_before_refill`.

## Exact commands, results and bounded-work proof

Required command (card): 27 methods, 0 skips, OK:

```text
$ python3 -m unittest discover -s ops/fiber-f1be0929/tests -p test_fiber_refill.py
----------------------------------------------------------------------
Ran 27 tests in 0.031s
OK
```

Changed suites:

```text
$ python3 -m unittest discover -s ops/fiber-f1be0929/tests -p test_fiber_selection.py
Ran 24 tests in 0.023s
FAILED (errors=1)
```
The single selection error is the pre-existing `test_real_describe_verify_and_dependency_closure`,
which imports the external `skcapstone`/`skcoord` packages (not present in this
sandbox). It was already failing on the pristine base revision; there are no
new selection failures.

```text
$ python3 -m unittest discover -s ops/fiber-f1be0929/tests -p test_fiber_control.py
Ran 28 tests in 0.012s
FAILED (errors=1)
```
The single control error is the pre-existing
`test_describe_review_binds_candidate_and_worker_rechecks_same_source`, again
from the missing external `skcapstone` package. No new control failures.

Bounded-work proof in tests:
- `test_more_than_32_recorded_entries_do_not_block_and_are_not_decoded` creates
  33 recorded-run qualification files and asserts `read_cards` is called once
  with exactly `['1234abcd']`.
- `test_controller_decodes_only_a_bounded_fair_queue_page` creates 40
  recorded-run queue names (containing invalid JSON that must never be decoded)
  and 40 unlaunched names, then asserts exactly one `MAX_INPUTS` page
  (`00000028`…`00000047`) is decoded and `cards` is called once.
- `test_cursor_advances_past_a_page_of_bad_names_across_fresh_invocations`
  asserts at most 1 native read per fresh pass and `<= MAX_INPUTS` total, then
  reaches the eligible card.
- `test_cursor_wraps_and_is_deterministic_on_repeat` asserts the second cursor
  is lexically earlier than the first (deterministic wrap).
- `test_dry_run_does_not_write_a_cursor` and
  `test_interrupted_cursor_publication_never_replays_work` assert no cursor or
  duplicate job is written by a preview or an interrupted publication.

Compile check (all six changed Python files): `python3 -m py_compile ...` → OK.

## Restart / rollback behavior

- Missing cursor → scan starts at the lexical beginning.
- Interrupted cursor publication (`os.replace` crash) leaves the previous
  cursor and no partially written cursor; the next pass re-examines and cannot
  duplicate work because `_publish` is no-clobber and `dispatch_one` refuses a
  duplicate/retained run.
- Terminal queue archival is idempotent and byte preserving via `os.link` +
  `unlink`; a crash after link but before unlink is recovered on the next pass.
- Rollback: the change is a local candidate commit only; reverting that commit
  restores the prior refill/selection/dispatch behavior. No deployed state,
  board data, database or worker is mutated by this candidate.

## Limitations (environment)

- `skcapstone` and `skcoord` are not installed in this sandbox, so the two
  pre-existing tests noted above cannot run here. This is an environment
  limitation, not a pass assertion; the external required test plane runs the
  prequalified refill command against the exact candidate.
- `pytest`, `black`, `flake8`/`pyflakes` and bwrap namespaces are unavailable,
  so the broader suite and the lint tier could not be executed here. They are
  reported as not run, never as passed.
- The configured base ref `work/cfa0416c-test-plane` is not present locally;
  binding was verified against the card's `base_revision` and the sandbox HEAD.
- This document does not claim hosted CI, deployment, native card completion or
  a whole-fleet trial.

## Candidate commit

Authorized local candidate commit (not pushed, not deployed):

```text
commit 40f5f7a01cb0c52afc44951779675009dea66303
tree   a120c30936bfb740ba2e06f271ab687718ea24f8
ref    refs/heads/work/fiber-a31d4e54-855850ab
parent 5f621b47bfe98efe4bdee59282cb06ba5c20bf7b
```

The candidate contains exactly the eight scoped paths above and nothing else.
Because this evidence file names its own commit hash, the revision that first
introduces the corrected hash is a follow-up evidence-only commit whose parent
is the candidate above; the follow-up changes no source or test bytes, so the
candidate tree `a120c309...` is the exact reviewed artifact. Nothing is pushed
and nothing is deployed. The external required test plane runs the
prequalified refill command against the candidate tree / follow-up revision
for independent cross-machine review.
