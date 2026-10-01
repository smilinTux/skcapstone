# c1a30122 fleet component completion evidence

Owner: `codex-worker-continuation-c1a30122`.
Claim: `d866e5fe3bc040a9bdf7c62710f2a7e3`.
Base: `855b5d8a52963fc419c69c5c99fca5972b9a368e`.

## Scope and implementation

The production worker environment explicitly assigns both Git author and
committer from the validated assigned worker owner. Names equal that owner;
emails equal `owner@noreply.invalid`. The domain is non-delivery machine metadata.
The exact clean environment is checked with both Git identity commands before
launch. No human or global Git identity is copied or configured.

`fleet builder-continue` defaults to a custody-only check. Applying it preserves
the complete isolated source workspace and creates an immutable private sidecar
grant. It binds original request bytes, all status fields except heartbeat_at,
exact claim and native revision, original BLOCKED event, Git source identities,
index hash, source inventory and archive. One hour expiry and one-use consumption
are enforced through the existing dispatcher, route/resource checks and locks.
The request, original outcome and claim remain unchanged. Clean-base retry keeps
its previous rules; only its existing custody validation is shared.

The fresh worker receives a continuation prompt requiring inspection of staged
work, correction of inaccurate draft test chronology, remaining checks and the
already authorized commit and handoff. An old BLOCKED outcome, missing receipt or
lost launch response cannot release the continued generation's source custody.

## Validation

Commands run from this isolated worktree:

```text
pytest -q tests/fleet/test_builder_continue.py tests/fleet/test_worker_git.py tests/fleet/test_builder_retry.py tests/fleet/test_production_builder.py tests/fleet/test_production_brief.py tests/fleet/test_builder_dispatch.py tests/fleet/test_builder_retire.py tests/fleet/test_production_exit.py
207 passed, zero failures or skips

ruff check <all changed Python source and test files>
All checks passed

python -m compileall -q <all changed fleet source modules>
passed

git diff --check
passed
```

Coverage includes actual Git identity resolution against a configured human
global identity without changing that configuration, actual read-only sandbox
Git inspection, staged and untracked byte preservation, unchanged request bytes,
native dispatcher one-use launch, no rematerialization or claim release, changed
claim/status/outcome/request/source/receipt, live and unknown processes, expired
grants, private archive permissions, missing reports, old-claim outcomes and
spent grants after launch failure. Heartbeat-only updates do not invalidate a
grant. The CLI defaults to checking only.

## Component provenance and limitations

The guarded card component_source_contract authorizes a separate coordination
component based on `217a5cd00e7de7ce3862f6f37180599657cc79a1`. Its typed verdict
validation is not copied into this older fleet tree. Both exact candidates need
independent review. The root operator verifies installed module hashes and the
composed runtime; this evidence does not assert one unified package tree.

No live service, worker, board claim, protected corpus, application source or
deployment was changed. The original a8300e02 source and malformed historical
verdict remain untouched. Full workspace preservation is bounded by the existing
2 GiB and 200,000-entry limits and can be expensive for large dependency trees.
Continuation requires a named branch still at the original base, useful dirty
work, the same retained claim, and a stopped production unit. Committed candidates
continue through existing review custody instead.

Consumption is persisted before launch. A crash there spends the grant and needs
separately authorized diagnosis; automatic replay is intentionally unavailable.
No live continuation or completion has been claimed. Acceptance still requires
the real worker handoff, exact candidate tests and independent review.

## Rollback and handoff

No data migration occurred. Revert these component source changes or restore the
root operator's installation preimages if later installed. Preserve sidecars,
archives, source workspaces and exact claims. Do not roll back a running worker
by deleting its consumption receipt. A private bundle and JSON descriptor bind
the final external commit/tree and every changed file without a self-reference
inside this report. The parent operator controls review, installation and use.
