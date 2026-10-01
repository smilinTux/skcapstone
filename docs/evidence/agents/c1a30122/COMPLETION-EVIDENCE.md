# c1a30122 coordination component completion evidence

Owner: `codex-worker-continuation-c1a30122`.
Claim: `d866e5fe3bc040a9bdf7c62710f2a7e3`.
Base: `217a5cd00e7de7ce3862f6f37180599657cc79a1`.

The guarded card component_source_contract authorizes this separate exact
coordination predecessor. Its coord.py SHA-256 before the patch was
`ba9d7d1951ac86c105785cc17a65363bdbce21d93bfeca7a85f41f250e7b9c20`, matching
the installed file and reviewed c1a30114 descriptor. Existing PR association,
guarded verdict and completion behavior stays on that predecessor history.

The patch calls the existing validate_blocked_verdict validator before either
guarded or unguarded coord verdict can append a native event. An unsupported
git_identity category now fails at the write boundary. A precise
blocked_on=capability referent=git-author-identity outcome remains valid and
retains its typed source binding.

Validation run in this exact isolated component worktree:

```text
pytest -q tests/test_coord_blocked_contract.py tests/test_guarded_candidate_verdict.py tests/test_provisional_verdict_producer.py tests/test_historical_pr.py tests/test_blocked_verdict.py
132 passed in 4.36s, zero failures or skips

ruff check src/skcapstone/cli/coord.py tests/test_coord_blocked_contract.py
All checks passed

python -m compileall -q src/skcapstone/cli/coord.py
passed

git diff --check
passed
```

Tests exercise actual guarded and unguarded CLI invocation, no native event on
invalid categories or missing referents, unchanged claim ownership, valid typed
BLOCKED evidence, existing candidate guards and PR association regressions.

No original outcome is rewritten and no live board, service or workspace is
modified. No data migration occurs. Rollback is the narrow component revert or
restoration of the exact installed preimage after any later backed-up install.
The separate fleet component has base
`855b5d8a52963fc419c69c5c99fca5972b9a368e`. Both candidates require independent
review; the root operator verifies composed installed hashes and runtime tests.
This report makes no unified Git-tree or package deployment assertion. A private
bundle and JSON descriptor bind final commit/tree and changed file digests.
