# c1a30124 retained production candidate review admission

Status: implementation verified; independent review and root installation pending.
Owner: `codex-production-review-c1a30124`.
Claim: `6839bc7e87d24a43b252331e8f035bb3`.
Dependency `c1a30122` was DONE before implementation.
Base: `05bf1920bd15d475f945f6b0f08507fc850a7b96`.
Ref: `refs/heads/feature/c1a30124-review-opener`.

## Change and acceptance evidence

The opener previously rejected every claimed parent before inspecting its
candidate. It now permits the production authority to consider a retained
source-only claim through `reviewable_source_candidate`. Existing native
validators bind the current owner/claim, typed outcome and timestamp, source
request and labels, current policy, transferred bundle and evidence hashes,
successful native terminal receipt, and current exact unit/process state.
The helper performs reads only and rechecks the native card revision.

The existing generation and duplicate-review gates still run. Ordinary claimed,
held, void, done, ambiguous, stale and malformed inputs remain excluded. Neither
`production_acceptance` nor `close_reviewed_parents` was changed. Downstream
provider independence, required tests and controller acceptance remain binding.

Files changed:

- `scripts/fleet/skfleet-rotate.py`: claimed production admission at the existing opener.
- `src/skcapstone/fleet/production_custody.py`: narrow read-only candidate custody helper.
- `tests/fleet/test_production_custody.py`: real native claim/Git transfer through existing
  opener creation exactly once, retained source events, and 21 refusal mutations.
- `docs/fleet/production-pi-workers.md`: retained-claim review operating rule.
- `changelog.d/c1a30124-review-opener.md` and this evidence file.

## Validation

Executed from the isolated worktree using Python 3.12.3:

```text
PYTHONPATH=src /home/skuser01/.skenv/bin/python -m pytest tests/fleet/test_production_custody.py tests/test_skfleet_provisional_opener.py tests/fleet/test_production_review_custody.py tests/fleet/test_production_review.py -q --tb=short
121 passed in 6.72s; zero failures, errors or skips.

/home/skuser01/.skenv/bin/ruff check src/skcapstone/fleet/production_custody.py tests/fleet/test_production_custody.py
All checks passed.

/home/skuser01/.skenv/bin/black --check src/skcapstone/fleet/production_custody.py tests/fleet/test_production_custody.py
2 files would be left unchanged.

/home/skuser01/.skenv/bin/python -m py_compile scripts/fleet/skfleet-rotate.py src/skcapstone/fleet/production_custody.py
Exit 0.

git diff --check
Exit 0.
```

The initial regression failed because the custody helper did not exist.
The existing opener tests preserve ordinary open-card behavior, source
generation invalidation, candidate validation and duplicate controls. Real
systemd terminal-check tests and independent-provider tests also pass.

An additional run including `tests/fleet/test_production_acceptance.py` returned
121 passed and 14 setup errors. Every error preceded test execution at the
historical `e6d82b82/AUTHORITY-DEPENDENCY.json` pin for installed `cli/coord.py`:
expected `77970a42e7b35dab6b548ede243fd6d940ea355b442767891e01877a713c5dd8`,
actual `182493e7cb990b62804cecfd423be20a13384edb25bbe69c9ab8151fb3170fc8`.
The root identifies the current installation as qualified by completed card
`c1a30122`. This task did not rewrite the historical pin, bypass its assertion,
or claim those 14 acceptance tests passed.

Read-only actual-candidate qualification used the current production policy,
native process snapshot and remote exact-unit check. Source `a8300e02`, owner
`pi-codex-builder-node-chiap02-a8300e02`, retained claim
`8c90a530788c4d41ba2269a0f9d3b2e8`, candidate
`393ce1765245f3bcf4d51f9fddeb4a9ccae2ddd4`: eligible was true, and source events
were unchanged. No review was created and no source or claim was mutated.

## Custody, limitations and rollback

Private evidence is under `~/.skcapstone/evidence/work/c1a30124/`, directory
mode `0700`, with owned regular evidence files mode `0600`. The external
`REVIEW-DESCRIPTOR.json` binds the final commit/tree/ref, all changed file hashes,
complete diff, review context and bundle, avoiding a self-referential commit.
`FOCUSED-TESTS.txt` and `ACTUAL-CANDIDATE-CHECK.json` retain observed results.
The exact native card is `c1a30124`; its evidence link names this file.

No data migration or installation occurred. Root must independently review the
candidate, retain hash-bound installed preimages, preserve the installed Python
shebang transformation, and verify the installed path before accepting the card.
Rollback is restoration of those exact installed preimages. Source rollback is
returning to the recorded base. Claims, source candidates and prior evidence must
be preserved. No push, deployment, protected-corpus work, outbound messaging,
claim release or canonical completion was performed.
