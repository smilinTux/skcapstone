# c1a30126 canonical production review candidate

Status: implementation and focused verification complete. Independent source
review, root installation, runtime calibration and actual production acceptance
remain pending. Owner `codex-canonical-review-c1a30126`, claim
`35c5f46f32fe40208c55b44593bdc82e`. Dependency `c1a30124` was DONE before edits.
Base `090a71d29217aeb6f18498335d2d3e2f0cf3f3f4`.
Ref `refs/heads/feature/c1a30126-canonical-review`.

## Cause and scope

The retained-source opener used legacy `coord create`, giving production a
different review identity and no repository/base/source-only metadata. The
source selector therefore returned no Git checkout contract. The downstream
acceptance path already requires the canonical native review identity.

The production branch now reuses exact custody checks, resolves the authorized
native review identity before directory guards, captures current source and
claim revisions, and delegates creation to `coord review-work --agent link`.
It revalidates custody before the call and checks complete native metadata,
workspace binding, lineage, review column and absence of active duplicates
afterward. Failed readback blocks admission in the current cycle. Existing
canonical directories are never overwritten. Legacy nonproduction behavior,
provider independence, source transport and completion gates remain unchanged.

Changed files:

- `scripts/fleet/skfleet-rotate.py`: production selection and native creation.
- `tests/test_skfleet_provisional_opener.py`: qualified dependency fixture and
  extraction of the production opener functions.
- `tests/fleet/test_production_custody.py`: retained-custody selection assertion
  now consumes the canonical plan; native creation moved to integration coverage.
- `tests/fleet/test_canonical_review_opener.py`: real guarded CLI, exact Git
  transport, actual source selector, reviewer brief and acceptance joins.
- `docs/fleet/production-pi-workers.md`, changelog fragment, plan, dependency pin
  and this evidence.

## Dependency provenance

`AUTHORITY-DEPENDENCY.json` pins six authority modules including `cli/coord.py`,
`guarded_review_work`, `link_review_work`, `review_work_identity`,
`review_replacement` and `seraph_review_cardstore`. Every installed module was
byte-compared with its committed blob at reviewed coordination source
`3b36c0333161e9e03c6d38695bf5ed388c9db6c1`. Completed card `c1a30122` records that
component, its independent review and operator installation; its predecessor
is `217a5cd00e7de7ce3862f6f37180599657cc79a1` from the `c1a30114` history.
No overlay implementation was copied into this source tree. Integration tests
fail on changed dependency bytes, load the pinned API explicitly and execute
the installed native CLI against only a temporary synthetic CardStore.

## Tests and acceptance evidence

Python 3.12.3, exact isolated candidate worktree:

```text
PYTHONPATH=src /home/skuser01/.skenv/bin/python -m pytest tests/test_skfleet_provisional_opener.py tests/fleet/test_canonical_review_opener.py tests/fleet/test_production_custody.py tests/fleet/test_source_bundle.py tests/fleet/test_production_source_review_brief.py tests/fleet/test_production_review_custody.py tests/fleet/test_production_review.py tests/test_skfleet_review_closer_generation.py -q --tb=short
181 passed in 33.51s. Zero failures, errors or skips.

/home/skuser01/.skenv/bin/ruff check tests/fleet/test_canonical_review_opener.py tests/fleet/test_production_custody.py tests/test_skfleet_provisional_opener.py
All checks passed.

/home/skuser01/.skenv/bin/black --check tests/fleet/test_canonical_review_opener.py tests/fleet/test_production_custody.py tests/test_skfleet_provisional_opener.py
3 files would be left unchanged.

/home/skuser01/.skenv/bin/python -m py_compile scripts/fleet/skfleet-rotate.py
Exit 0.

git diff --check
Exit 0.
```

The new regression failed before the fix with `0 == 1`: a voided legacy review
directory prevented canonical creation. The completed integration proves one
canonical card, idempotent subsequent opener cycles, source-only metadata,
unchanged source events/claim, private clean Git checkout at the exact unpublished
head/tree, successful production source-only brief generation, committed
synthetic independent-review evidence and successful `production_acceptance.collect`.
The acceptance join retains source and reviewer claims; it does not complete
either card or substitute model claims for trusted required-test receipts.

Refusals cover active legacy review, live/unknown process, hold, missing native
API, source and claim races at the CLI boundary, malformed success output,
success with no card, source binding corruption and a concurrent duplicate.
Existing custody tests cover 21 stale/malformed/unknown handoff variants; provider
family, systemd custody, brief and source-bundle regression tests also pass.

## Limitations and rollback

The integration substitutes only external process/route qualification observations
with explicit synthetic fixtures; separate focused tests exercise those boundaries.
It is not live production qualification, independent source review or required-test
controller acceptance. Historical acceptance fixtures pinned to an older authority
installation were not rewritten; this task supplies a new current pinned
composition test instead. No Python version other than 3.12 was claimed.

The only runtime delta is the rotation script. Root must verify its installed
preimage, preserve its installed interpreter shebang, back up exact bytes and
mode, install only the independently reviewed body delta, then requalify the
composed runtime and observe automatic canonical checkout/review/test/completion.
Rollback restores the exact installed script backup and applicable runtime
qualification state without deleting evidence or changing source custody.

No data migration, live review creation, source mutation, claim release, push,
installation or deployment was performed by this worker. Root separately owns
native disposition of the malformed historical review. Private bundle and exact
commit/tree/file hashes are in the external review descriptor under
`~/.skcapstone/evidence/work/c1a30126/`. Native task `c1a30126` links this evidence.
