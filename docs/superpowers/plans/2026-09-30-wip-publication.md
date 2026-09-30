# Guarded WIP Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish qualified source-only candidates through a narrow WIP PR and exact-head CI boundary, without changing the existing scheduler.

**Architecture:** Reuse the existing private Forgejo transport, retaining its default write restrictions. Add a dedicated WIP-only client and an idempotent exact-candidate publication function. The operator supplies qualified immutable source/review lineage; workers never get credentials.

**Tech Stack:** Python standard library, existing pytest and Forgejo API.

**Spec:** `docs/superpowers/specs/2026-09-30-wip-publication.md`

## Global Constraints

- Only private SKLegal, exact `WIP: ` prefix, immutable branch, `main` base.
- No automatic merge, deployment, protected Matter egress or legal actions.
- Existing dispatcher, shared 12-worker ceiling, provider caps and 75-second launch spacing remain unchanged.
- Do not enable inactivity-only claim expiry or legacy lifecycle timers.
- Preserve source, evidence and rollback. Never turn absent CI into success.
- Execution skills named above are not installed; execute inline with focused tests and governed independent review.

---

### Task 1: Draft-only transport and reconciliation

**Files:** Modify `src/skcapstone/forgejo.py`; create
`src/skcapstone/wip_publication.py`, `tests/test_wip_publication.py`, and
`changelog.d/9897319a-wip-publication.md`.

**Interfaces:** `ForgejoWipClient.request(method, path, payload=None)` uses the
existing bounded transport, but writes only `POST /repos/smilinTux/sklegal/pulls`.
`publish_wip(client, candidate)` validates a frozen `WipCandidate`, reads current
branch heads and open PRs, creates at most one WIP PR and verifies its readback.

- [ ] Write parameterized tests: default client rejects PR creation; WIP client
  rejects merge/review/PATCH/DELETE, non-WIP titles, extra fields and unsafe refs.
  Use the existing `Opener` fixture to prove rejected operations do not hit the network.
- [ ] Run `PYTHONPATH=src /home/skuser01/.skenv/bin/python -m pytest -q tests/test_wip_publication.py` and record the expected missing-interface failure.
- [ ] Extract only the bounded HTTP send into `ForgejoClient._send`; keep the
  existing `request` validation unchanged. Add draft-only validation before `_send`.
- [ ] Test deterministic branch identity, exact source/review lineage marker,
  base/head drift, duplicate/conflicting PRs, lost-response reconciliation,
  exact readback and empty/failing/pending CI. No Git or board writes in this layer.
- [ ] Run the new tests plus `tests/test_forgejo.py`, `tests/test_seraph_forgejo.py`
  and `tests/test_link_observation_producer.py` to check the existing boundaries.
- [ ] Commit only the named source/test/docs/changelog files locally; echo
  `git rev-parse HEAD` and record the exact tree for independent review.

### Task 2: Operator qualification and bounded live canary

**Files:** Evidence under `~/.skcapstone/evidence/work/9897319a/`; no worker
credential files or live lifecycle manifests are changed.

- [ ] Verify the candidate bundle and independent review bytes in fresh trusted
  Git metadata; compare current remote base and all publication ancestors.
- [ ] Dispatch one governed cross-provider review through the existing shared
  capacity gates. Do not activate this candidate until that review passes.
- [ ] Check existing CI workflow and branch protections; publish only a qualified
  exact head under its immutable WIP branch, never update another branch.
- [ ] Run the publisher twice. Require one identical PR, exact head/base and
  `WIP: ` title on both readbacks. Observe actual exact-head CI statuses.
- [ ] Record actual result, limitations, source hashes and rollback. Keep PR WIP,
  no merge/deploy. If source binding or review is incomplete, leave it unpublished.

### Deferred activation gate

Automated inactive-claim handoff and continuous queue refill require a separately
tested adapter to the existing lifecycle. This increment must not claim full
unattended W7 completion or enable timers for capabilities it does not implement.
