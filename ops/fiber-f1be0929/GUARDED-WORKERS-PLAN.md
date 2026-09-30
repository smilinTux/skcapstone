# Guarded Workers Implementation Plan

> Execute inline with test checkpoints. The executing-plans and
> subagent-driven-development skills are not installed in this session.

**Goal:** Resume only qualified source work through one guarded dispatcher.

**Architecture:** The approved trusted controller owns claims, credentials,
admission and native board writes. The model runs in a fresh private filesystem,
PID and network namespace with only its standalone task repository writable.
A per-task Unix socket exposes only the pinned SKGateway completion route;
the private network bridge cannot reach arbitrary hosts. Model proposals never
authorize card completion. Existing work and ambiguous launches remain held.

**Tech Stack:** Existing Python stdlib, systemd, Git, Pi and bubblewrap.

**Spec:** Human-approved guarded-restart design in the conversation, recorded
on card 11c169d6 under parent 3ee76566.

## Global constraints

- Estate ceiling 9, launch spacing 75 seconds, existing provider caps unchanged.
- No model-visible host credentials, host management sockets or host network.
- No global user-namespace sysctl changes, Docker grants, push, merge or deployment.
- Fail closed when sandbox support or runtime dependencies are absent.
- Preserve paused worktrees, active sessions, claims and immutable run history.
- Board updates use native coord, exact owner and revalidated claim generation.
- Source-only synthetic tasks; protected Matter processing remains prohibited.

## 1. Private execution boundary

Files: fiber_sandbox.py, fiber_gateway_bridge.py, tests/test_fiber_sandbox.py.

- [ ] Write refusal and bridge tests before implementation.
- [ ] Build a curated bubblewrap root using /usr, private /proc, /dev, /tmp,
      private HOME, read-only Pi runtime and only the task repo writable.
- [ ] Unshare network and all other supported namespaces; drop capabilities,
      disable nested user namespaces and require NoNewPrivs in kernel readback.
- [ ] Proxy only POST /v1/chat/completions for the exact assigned model. Keep
      gateway auth in controller memory; disable redirects and environment proxies.
- [ ] Supply a read-only source/claim snapshot; controller rechecks authority
      before upstream requests and after execution. No raw CardStore access.
- [ ] Run OS denial tests on ZIOWK01, which passed the namespace preflight.

## 2. Worker and admission integration

Files: fiber_worker.py, fiber_admission.py, existing worker/admission tests.

- [ ] Materialize a standalone clone with no shared Git alternates or writable
      cache metadata. Refuse an occupied path, never clean it automatically.
- [ ] Run Pi with no discovered extensions or inherited host environment.
- [ ] Link controller-observed execution evidence through native coord, without
      promoting model output to PASS, completing cards or releasing claims.
- [ ] Change and test ESTATE_CAP == 9; qualify sandbox before launch admission.

## 3. Guard unattended control

Files: fiber_control.py, tests/test_fiber_control.py, compatibility babysitter.

- [ ] Bound repeated failing preflight attempts; persist a HOLD after three.
- [ ] Keep capacity waits distinct from faults; never retry ambiguous launches.
- [ ] Route the old babysitter through the sole managed dispatcher, with no
      direct Herdr prompts or independent worker starts.
- [ ] Verify HOLD and both per-host/provider limits under repeated timer cycles.

## 4. Review and operations

- [ ] Run the complete fleet suite and live sandbox denial tests.
- [ ] Create the card-authorized local-only snapshot and independently review
      exact source bytes. No push, merge or hosted-CI success claim.
- [ ] Install reviewed bytes with exact backups under the parent operations card.
- [ ] Enable a canary only after admission capacity and isolation qualify.
- [ ] Verify useful task output, independent review and restart behavior before
      declaring unattended operation ready. Keep unqualified hosts held.

Rollback: retain HOLD, restore exact backed-up adapter files, preserve task
repositories and receipts. Never restore the unsafe blind-prompt babysitter
to active operation. Capacity cannot be released solely because time elapsed.
