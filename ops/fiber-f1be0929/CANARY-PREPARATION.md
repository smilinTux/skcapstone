# Source-bound canary and native artifact transport

Parent f1be0929, jarvis, 2026-09-29 22:03 UTC.

## Held implementation card

Created card 4ce90fdd with source-only, dispatch-approved, parent-f1be0929,
and do-not-claim labels. It is unowned in backlog. Native coord gates confirms
eligible=false, reason do-not-claim. No claim or worker launch occurred.

The card is the assigned TDD for a tests-only implementation on chiap02:
prove the native report-only builder gate preserves an unexpired pending
offer, existing status bytes and claims, while heartbeat publication continues;
also test the reversible native flag change. Tests must use scratch fleet
state and the source checkout, with no production module or live fleet edits.
A local test/evidence commit is expressly requested; push, PR, merge,
application deployment, credentials, and protected Matter content are excluded.
Acceptance includes a disposable mutation test that removes the admission
guard, proving the new test actually detects the missing protection.

Repository: https://github.com/smilinTux/skcapstone.git
Published main revision observed via git ls-remote:
c6031047328348aca53dc3611cf94ceb56a9006c
Tree: 995927649c48fce2f2674ea4e14a259b0b8dc2c4

Prepared request: canary-implementation.request.json, kept in this evidence
directory, NOT the live dispatcher queue. Its fingerprint includes the hold
label. Regenerate it from the mediated card after any deliberate label or
contract change; do not hand-edit the fingerprint to bypass a changed task.

## Minimal adapter change

The worker previously always fetched the pinned commit from origin. A private
candidate created by implementation and handed to review through a Git bundle
would not exist on origin, even though the exact object had already arrived.

ensure_commit now reuses an already imported exact commit in the repository
cache. It still requires a 40-hex revision, verifies the object is a commit,
and fetches only that exact origin SHA if absent. Existing cache-origin,
task fingerprint, claim, and new-worktree/branch guards still run. It does not
accept an arbitrary tree/blob or change source bindings. There is no new
archive format, package, daemon, remote push, or automatically trusted branch.

Installed fiber_worker.py hash on chiap08:
42553364356a590b2ed833c3209a0461bcf02140762b18a36aa4c6ad479148f6
The same file was deployed to chiap02/03/04. Prior versions are retained as
fiber_worker.py.before-bundle-cache on each target and
backup/fiber_worker.before-bundle-cache.py beside this report. Hold admission
before any exact-hash-fenced restore. No running worker was restarted.

## Tests and actual transfer

- Local suite: 48 tests passed in 1.138 seconds.
- chiap04 suite: 48 passed in 0.307 seconds. Unit
  f1be0929-controller-tests-r8-X6sJff.service, invocation
  ef54ec4268404069a97190393ddd62d7, exit 0, runtime 677 ms.
- New tests use real Git repositories and bundle import. They prove exact
  commit/tree/content preservation, no origin fetch for imported candidates,
  rejection of a blob masquerading as a source revision, and exact-SHA fetch
  for a missing commit. Ruff F checks passed.
- A complete-history bundle of the published canary base was created in an
  isolated bare repository, without changing the existing dirty checkout.
  Source: /tmp/f1be0929-bundle.VFVwBC/canary-base.bundle, about 8.6 MiB.
  SHA256: b3037b2d1a8c9360fdc6628591240241e82deebffbd594cba012e935a82c8bb4.
- That bundle was transferred over existing SSH to both chiap03 and chiap04.
  Each independently ran git bundle verify, imported it into an isolated bare
  repository, and returned the exact same bundle SHA256, commit and tree.
- chiap03 receiver: /tmp/f1be0929-bundle.oVlKmS/receiver.git.
  Unit f1be0929-bundle-oVlKmS.service, exit 0, runtime 478 ms.
- chiap04 receiver: /tmp/f1be0929-bundle.YceI6F/receiver.git.
  Unit f1be0929-bundle-YceI6F.service, invocation
  2ee950ba590742d3b75c50b0167c3c3b, exit 0, runtime 642 ms.
- Both transfer/import checks used CPUQuota=200%, MemoryMax=1G,
  MemorySwapMax=256M, TasksMax=64, RuntimeMaxSec=120.

This proves source artifact transport, not agent implementation or independent
review. The transferred bundle contains the published base, not a completed
canary candidate. No task worktree was created or accepted by these probes.

## Remaining sequence

1. Complete independent infrastructure review, reconcile remaining admission
   and recovery gates, and establish capacity below the estate/provider caps.
2. Remove only the intended canary hold, regenerate its bound request, obtain
   fresh route qualification, and admit implementation on chiap02.
3. Verify the candidate locally from exact commit/tree, changed paths, tests,
   evidence hash and current board state. Preserve all artifacts on failure.
4. Export that exact candidate as a Git bundle. Verify its hash and commit
   on chiap03/04, then import under the same per-repository cache lock used by
   workers. Create a genuinely governed independent review card only once the
   producer identity, outcome generation and evidence/commit hashes exist.
   Use a qualified reviewer identity and different provider family, preserving
   all native review and CI evidence gates. Never fabricate hosted checks.
5. Run the assigned isolated test stage and independently verify all outputs
   and board outcomes before accepting the rollout.

The legacy verify-card.sh must be run as required, but is not sufficient by
itself: its board parsing currently emits a warning without failing. External
acceptance must independently inspect exact candidate and scoped board state;
do not infer completion from that helper's exit code alone.

The native coord-gates capacity object is a logical review-route pool, not the
estate-wide worker ceiling. Its displayed target=52 must not be treated as
permission to exceed nine. Live physical occupancy remains the admission gate.
New admission is still held and the dispatcher timer remains disabled.
