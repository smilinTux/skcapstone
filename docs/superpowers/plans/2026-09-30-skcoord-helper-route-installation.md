# Approved SKCoord helper-route installation

Card: 52a411a2. Owner: jarvis. User approved reviewed installation and rollback.

Goal: unblock the preserved f7f6daeb crew without altering its claim or workspace.
Reuse b36dba96's preserved candidate, not a new orchestration implementation.

1. Preserve exact original source hashes and claims. Seal an isolated local-only
   review snapshot of the approved candidate. Do not modify the original dirty
   b36dba96 workspace or act as its owner.
2. Reproduce the old failure and candidate pass; run focused, adjacent, and
   cross-package tests, all six f7 nodes, and the exact inherited fleet baseline.
   Run formatting and diff checks. Preserve complete results, including failures.
3. Obtain independent cross-provider review of exact snapshot and installation
   scope. No extra Codex worker, no protected matter content, no worker secrets.
4. Compare installed package against candidate. Back up the exact changed
   boundary, install only approved reviewed bytes, and verify import hashes and
   the same tests. Roll back immediately on regression. No unrelated upgrades.
5. Record evidence on native card 52a411a2. Recheck f7 gates and resume only
   eligible work within shared capacity, spacing, ownership and source controls.

No push, merge, application deployment, claim theft, session kill, credential
copying, provider-cap increase, or automatic acceptance of model assertions.

Current evidence: 16c3cd09/PAUSED-HELPER-BOUNDARY.json records installed helper
failure (three tests), preserved paired candidate pass (three tests), and 81
focused passing tests. This is not full acceptance or independent review.
