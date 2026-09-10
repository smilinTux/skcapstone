# Fleet Lessons Learned: 2026-09-03/04 Operational Session

Status: distilled from a multi-hour fleet stabilization session. These are
permanent workflow changes, not one-time fixes. Each lesson includes the
incident that taught it, the permanent fix, and where the fix lives.

## 1. Never deploy uncommitted bytes

**Incident:** The provider-purity fix was reported as "deployed" but existed
only as uncommitted working-tree changes. The live gateway never ran them.
Caught by independent audit (twice).

**Rule:** `git status` must be clean before any deploy claim. If the diff
exists only in a worktree, it does not exist.

**Fix:** Process discipline. The `skfleet-rotate.py` deploy path now
requires a committed hash.

## 2. Deleting a PR head branch auto-closes the PR

**Incident:** PR397 auto-closed when its head branch was deleted during
cleanup. The PR could not be reopened and required a full reland (PR419).

**Rule:** Never delete the head branch of an unmerged PR. Verify
`state=MERGED` in the API before any branch cleanup.

**Fix:** Process discipline. Check `gh pr view <N> --json state` returns
`MERGED` (not `CLOSED`) before `git push --delete`.

## 3. `mergeStateStatus` must be CLEAN, not just checks-green

**Incident:** A PR had all checks passing but `mergeStateStatus=BEHIND`
because main had moved. The merge silently failed.

**Rule:** `pending=0` is necessary but not sufficient. The branch must be
`CLEAN`, which requires it to be up-to-date with main.

**Fix:** Check `gh pr view <N> --json mergeStateStatus` before merging. If
`BEHIND`, merge main into the branch first, push, wait for green again.

## 4. The unit-name allowlist must include every lane

**Incident:** The kimi lane existed in the rotator but crashed with
`ValueError: invalid worker unit identity` on every launch because
`_worker_unit_name` had a hardcoded set `{"codex", "glm", "qwen",
"escalate"}` missing `"kimi"`. The crash was silent for hours.

**Rule:** When adding a lane, update ALL of: `_LANE_ONLY_LABELS`,
`_worker_unit_name`, `ordinary` tuple, `_LANE_RANK`, and the
`_parse_worker_units` regex.

**Fix:** Deployed. Future lane additions should grep for the lane name
across the rotator and verify all five touchpoints.

## 5. Provider purity must be opt-in per backend

**Incident:** An unconditional fail-closed gate broke three pinned
contracts (lifecycle 410 attribution, fast-failure quarantine, total
outage attempt-all). The independent review BLOCKED the first attempt.

**Rule:** Cross-provider routing changes that alter fallback behavior
must be opt-in via a config flag, not unconditional. Existing contracts
that depend on legacy behavior must be preserved by default.

**Fix:** `provider_purity: true` backend flag. Tests pin the precedence
rules (eol verdicts, claim-quarantine carve-out, total outage).

## 6. Unbounded regex on request content is a fleet-wide DoS

**Incident:** A 420KB single-string message drove the request classifier
into catastrophic regex behavior. The entire gateway froze - all endpoints,
including health checks. SIGTERM could not stop it.

**Rule:** Any code that processes arbitrary-length user content must
bound its input. Classification signals live at prompt head/tail.

**Fix:** 8K head + 2K tail bound on classifier input (PR110). 420K goes
from infinite to 114ms.

## 7. The trim boundary can orphan tool_calls

**Incident:** Fleet workers failed with OpenAI 400 "No tool output found
for function call" because the history trim repaired tool pairing only
in the tail slice. An assistant tool_call in the head slice whose reply
dropped in the middle reached upstream orphaned.

**Rule:** Always repair the final assembled message array, not just a
slice. The boundary between kept and dropped messages is where orphans
form.

**Fix:** All three trim passes now run `repairToolPairing` on the
assembled result (PR112).

## 8. Card descriptions must be written at creation time

**Incident:** A card was created with an empty description. A worker
claiming it received no task definition and could not proceed.

**Rule:** The description IS the task. If it is empty, the card is not
ready to create.

**Fix:** Process discipline. Always include `--desc` when creating cards.

## 9. Repair cards must embed their defect evidence

**Incident:** A card demanded repair of "three Ruff findings" that never
existed in any recorded evidence. The preflight gate correctly refused,
but the card wasted a worker slot and review effort.

**Rule:** Every repair card must carry: the exact command to reproduce
the defect, the observed output, and the hash of the bytes the defect
was observed on. The preflight gate should verify the premise before
any worker acts.

**Fix:** The `f4faf1fb` preflight pattern should be standard for all
repair cards.

## 10. Backoff is a stale artifact of lane outages, not card difficulty

**Incident:** 75 cards were in backoff because they failed on the glm
lane during a z.ai outage. After z.ai recovered, none of them were
retried because nothing cleared the backoff state.

**Rule:** Backoff triage must classify exit reasons by lane. Cards whose
failures are 80%+ on one dead lane should auto-pin to a healthy lane
when that lane recovers.

**Fix:** Carded as `2a456cb7` (autopilot). Interim: manual pinning
using the `codex-only` label.

## 11. Stale cards accumulate silently and pollute every scan

**Incident:** 648 non-terminal cards had zero events for 7+ days. 118
of them were CMDB drift duplicates of the same CI. They inflated every
selector scan, status projection, and coordination summary.

**Rule:** Cards with no activity beyond 2x the average cycle time
should be auto-triaged. Recurring alerts should update one tracking
card, not create new ones per occurrence.

**Fix:** Bulk sweep executed (369 closed). The autopilot card includes
staleness auto-triage.

## 12. Syncthing propagation lag can strand new cards

**Incident:** New cards created on chiap08 were invisible to the hosts
they hashed to for 30+ minutes because Syncthing had not converged.
Manual scp was required.

**Rule:** Card creation should verify propagation to all hosts within
60 seconds. If missing, fall back to direct SSH copy.

**Fix:** Carded in the autopilot (`2a456cb7`).

## 14. A completed card can respawn workers forever

**Incident:** Card 56f9d32f was completed by the coordinator at 21:26Z, but a
rotation host whose sync view predated the completion re-claimed it and spawned
a worker. Killing that worker made it worse: the worker exit trap appends
`release_claim`, and the claimability fold reset the card to `backlog`, so the
next 5-minute cycle spawned another worker. Two respawns measured (21:29Z,
22:29Z); each kill re-queued the card.

**Rule:** Terminal states are sticky for claim and release, not just assign
and unassign. A late claim is a sync-race symptom, not a revival. The only
sanctioned revival path is an explicit `reopen` event.

**Fix:** Fold guards in `skfleet-rotate.py` `_fold_claimability`
(card cfb9c863). Operator unblock for a card already stuck in the respawn
loop: append a terminal `move -> done` as the LAST event after the final
release (`skcapstone coord move <id> done --agent <you>`), then verify one
full rotation cycle spawns nothing.

**Evidence:** `evidence/work/56f9d32f/20260904T224500Z-RECOVERY-AUDIT-AND-CLOSEOUT.md`.

## Summary table

| Lesson | Incident cost | Permanent fix | Where |
|--------|--------------|---------------|-------|
| Uncommitted bytes | 2h rework | Deploy requires committed hash | Process |
| PR branch deletion | Full reland | Check MERGED before delete | Process |
| mergeStateStatus | Silent merge failure | Check CLEAN | Process |
| Unit-name allowlist | Hours of silent kimi crash | All 5 touchpoints per lane | Rotator |
| Provider purity opt-in | BLOCKED review | Config flag + precedence tests | PR108 |
| Regex DoS | Full gateway freeze | Input bound 8K+2K | PR110 |
| Trim orphans | Fleet 400 errors | Repair assembled result | PR112 |
| Empty descriptions | Worker no-op | Always include desc | Process |
| Unfounded repair premise | Wasted review | Preflight gate standard | Process |
| Backoff = stale outage | 75 cards stuck | Lane-aware triage | Autopilot |
| Stale card accumulation | 648 cards | Bulk sweep + auto-triage | Autopilot |
| Syncthing lag | Cards invisible 30min | Propagation watch | Autopilot |
| Completed-card respawn | 2 zombie workers, 12.5h burn | Terminal sticky for claim/release + move-done unblock | Rotator (cfb9c863) |
