# Source card 7ec78ece evidence

Scope: SKCapstone, SKDashboard, and SKWorld lifecycle merge authorization only.
SKLegal is excluded. No deployment, merge, release, settings change, or
external actuation was performed.

## Exact source

- protected base: `1683ca48eff1b44fb4c15451f2df7882be54ea80`
- implementation commits: `e2d19374`, `c87c6043`, and `a528dbdb218ed68c18e986b5b0d5ec540f62d47e`
- implementation head: `a528dbdb218ed68c18e986b5b0d5ec540f62d47e`
- implementation tree: `73e3207a4f8fe427cd69ec11239b4288b5fcf006`
- implementation patch SHA256, `git diff --binary BASE..HEAD -- src tests`: `1101e3e1e3508fd4950984384947f8fdfba9e7f771736136007db40ae297816a`
- branch: `fix/7ec78ece-merge-fence`
- isolated worktree: `/mnt/cloud/onedrive/projects/DAVE-AI/worktrees/skcapstone-7ec78ece`

The earlier source candidate was based on `5dc02cb0` and was superseded by
this exact current-main refresh. Earlier review `ec865455` is stale and is
not reused for this candidate.

## Changed file hashes

```text
30a012365c59ccc136959479fb96b071edf20ae5d9b84dd93a1dee925c60d2a5  src/skcapstone/link_merge_authority.py
f48e8ca5e0a0c4788d68247f6d8988183d5b6bfc810659bd166b747cc92cd75e  src/skcapstone/seat_boundaries.py
f9f8fbb67d668f0b44d52e48d29d6626ec44f726bc1dd22ee932311ea8822ee2  tests/test_link_merge_authority.py
```

## Acceptance evidence

- The shared Link authorization fence locks source and review CardStore cards
  in stable order, rereads protected base, candidate head, tree, patch, merge
  state, and required CI, and then rereads terminal independent CardStore PASS
  state before returning a sealed receipt.
- Missing, nonterminal, stale, producer-owned, mismatched, and post-merge
  review states fail closed.
- The race regression holds the merge fence while terminalization attempts to
  proceed and proves the merge decision cannot observe a terminal review before
  CardStore terminalization materializes.
- PR582 regression records merge attempt after review materialization and
  terminalization timestamps, rejecting invalid or post-merge ordering.
- The existing recommendation-only decision remains actuator-free and the
  Link-only authority boundary remains intact.

## Verification

```text
ruff check src/skcapstone/link_merge_authority.py src/skcapstone/seat_boundaries.py tests/test_link_merge_authority.py
All checks passed!

python -m pytest -q tests/test_link_merge_authority.py tests/test_link_cycle.py tests/fleet/test_seat_boundaries.py tests/test_link_review_work.py tests/test_review_freshness_gate.py
161 passed in 1.81s

python -m pytest -q tests/test_link_merge_authority.py::test_final_fence_rereads_exact_state_and_seals_receipt tests/test_link_merge_authority.py::test_final_fence_rejects_stale_or_post_merge_review tests/test_link_merge_authority.py::test_final_fence_rejects_missing_or_nonterminal_cardstore_pass tests/test_link_merge_authority.py::test_pr582_merge_cannot_beat_review_terminalization
9 passed in 0.48s

python -m pytest -q tests/test_integration.py tests/test_integration_backbone.py
111 passed, 16 skipped in 11.99s

python -m compileall -q src/skcapstone/link_merge_authority.py src/skcapstone/seat_boundaries.py
git diff --check
```

The prior candidate's `151 passed in 6.15s` result is retained as historical
evidence for the stale-base candidate. The refreshed candidate was rerun from
the exact current protected main above.

Hosted CI was not triggered because this source card prohibits push and merge,
and no hosted PR exists for this isolated unpushed branch.

## Governance handoff

- producer seat: Tank
- source card: `7ec78ece`
- independent reviewer seat: Seraph
- exactly one new current review card must be created after this evidence is
  committed
- the stale review card `ec865455` must not be reused
- Tank does not author the review verdict
- zero-human-approval CardStore governance and Casey-directed Jarvis controls
  remain unchanged
