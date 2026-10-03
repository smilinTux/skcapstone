# Exact review candidate checkout

Card: `562afe2e`. Parent: `9b230773`.
Owner: `codex-review-checkout-562afe2e`.
Source base: `37011682fcc2351fada0f455c7db0a1c39e1ab6f`.

The generated production review keeps its producer's original `base_revision`
beside the reviewed `link_head_revision`. The launcher formerly passed the
base into both source bundle verification and import. A real unpublished Git
candidate regression reproduced `review source head binding invalid`.

After validating the original repository/base contract, the shared workspace
specification selects the exact candidate for a typed production review.
Existing binding validation refuses conflicting meta/link values. Producer
and legacy workspace selection are unchanged; original card metadata and
source custody remain untouched. No bundle or claim checks were removed.

Validation:

- Four regression cases failed before the fix, including the real bundle
  path and missing/malformed/conflicting candidate bindings.
- `python -m pytest -q tests/test_skfleet_workspace_materialization.py
  tests/fleet/test_source_bundle.py tests/fleet/test_source_transport.py`:
  73 passed, 0 skipped, 7.64 seconds.
- The real Git test runs preclaim then import, checks exact HEAD/tree and
  clean status, preserves producer ownership and the original base, and
  refuses fallback to remote candidate lookup.
- Ruff checks for both changed test files and `git diff --check` passed.
- Source-bundle fixture retains two pre-existing formatting-only missing
  trailing commas; no unrelated formatting cleanup is included.

No deployment, push, trial-card modification or provider call occurred.
Independent exact-candidate review remains required before installation.
Rollback of this source change is reverting the candidate commit. Live
installation must preserve its own exact preimage and rollback receipt.
