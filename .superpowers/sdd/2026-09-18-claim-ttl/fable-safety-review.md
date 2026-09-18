# Adversarial safety review: claim TTL reaper vs. live workers

Reviewer: Fable (adversarial pass, safety claim only). Date: 2026-09-18.
Scope: can any reachable sequence of events make this mechanism release a card a
LIVE worker is holding? Answer: **yes, confirmed by execution.** One blocker.

Probes live at
`/tmp/claude-1000/-home-cbrd21/5d6efef6-d5b9-4b73-baab-4b2958a79e1c/scratchpad/probe/`
(`p1_evidence_blind.py`, `p2_ts_ttl_mode.py`), run with `~/.skenv/bin/python`
against this worktree's `src/` and the live skcoord checkout.

---

## FINDING 1 (BLOCKER, CONFIRMED BY EXECUTION): the idle clock is blind to the
## evidence store, so a worker actively posting evidence is "idle"

`claim_expiry._last_owner_event_at()` reads only `cards/<id>/events/*.jsonl`.
But `coord link` — the verb that carries evidence, CI links, and **verdicts** —
writes exclusively to the legacy overlay `coordination/card_events/<host>.jsonl`
via `CardEventLog` (`src/skcapstone/cli/coord.py`, `coord_link`, ~line 1343: it
constructs a `CardEvent` and appends to `CardEventLog`; it never touches
`CardStore.append_event`). The fold itself DOES merge that overlay
(`load_legacy_mutations` in skcoord `card_store.py` ~line 370, `link` is in
`_OVERLAY_TO_STORE_ACTION`), which is why the module's own docstring believes
"a worker doing real work writes move, describe, evidence and verdict events
continuously". For evidence and verdicts, that premise is false at the one
place it matters: the idle measurement.

### The demonstrated sequence (p1_evidence_blind.py, run, output reproduced)

1. T-50h: worker `jarvis` claims card `c-live-1` (real `CardStore.create` +
   `mirror_coord_claim`; claim event has `writer=jarvis, owner=jarvis`,
   revision R).
2. T-40h .. now: the live worker posts evidence through `coord link`'s exact
   write path (`CardEventLog.append`, writer=`jarvis`), including a
   `verdict=PASS` written seconds before the reap.
3. The fold sees everything: `owner=jarvis`, `status=DOING`, and all four links
   including the fresh PASS.
4. `observe()` returns `last_owner_event_at = claim timestamp` (50.0h ago).
5. `evaluate(now, ttl=48h)` → `idle-beyond-ttl`, `reclaimable=True`.
6. The release event the reaper's CLI invocation would write
   (`release_claim`, `released_owner=jarvis`,
   `expected_claim_revision=R`) is appended; the fold's `releases_current`
   branch matches (same owner, same revision) and the card folds to
   `owner=None, status=BACKLOG`.

Output: `RESULT: LIVE WORKER LOST ITS CARD`.

Every step is a normal production event. No forged files, no clock games, no
malformed input. The worker was alive, writing, and seconds from completion.
Gated behind `SKFLEET_CLAIM_TTL_MODE=enforce`, but this is precisely the state
enforce is meant to be safe in, and report mode will systematically list these
workers as reclaim candidates (misleading the phase-2 gate in the OTHER
direction: an operator who spot-checks a few dead ones and sees them genuinely
dead will not learn that the live/dead discriminator itself is broken).

### Variants of the same hole (reasoning, same code path as the demo)

- **Review-wait:** worker moves card to `review` (refreshes clock), then waits
  on a reviewer. The reviewer's verdict arrives as `coord link` (invisible) AND
  under the reviewer's writer name (would not match `owner` even if visible).
  48h after the move, the card is reclaimed out from under a live worker whose
  card may already carry `verdict=PASS`.
- **Repo-work-only worker:** a worker whose real output is commits/files and
  who touches the board only at claim and complete. Cluster evidence of process
  lifetimes 61h, 251h, 376h means "claim … 48h of silence … complete" is a real
  profile; such a worker is reaped mid-run. This one is arguably the design's
  stated intent ("no events = no progress"), but the design justified that
  intent with "workers write evidence continuously", and Finding 1 shows the
  mechanism cannot see the evidence they write. The two findings cannot both be
  waved off: either evidence counts as activity (then read the store it lives
  in) or it does not (then the docstring and the rollout argument are wrong).
- **`coord link --agent` defaults to `""`** (host attribution). Even after
  fixing the read side, evidence posted without `--agent` will not match the
  owner. The fix must address the write-side default too.

### Fix

Measure owner activity over the same merged event view the fold uses:
`CardStore._read_events(card_id)` + the `card_events` overlay rows for that
card (match `writer == owner or owner-field == owner`, same predicate). That
is ~the fold's input, minus the fold. Alternatively expose
`last_event_at(card_id, writer)` from skcoord so there is exactly one reader.
Additionally: default `coord link`'s writer to the invoking agent identity, or
require `--agent`.

---

## FINDING 2 (CONFIRMED-BY-READING): the CAS fence does not protect the case
## that matters

The `--expected-claim-revision` CAS refuses only when the revision CHANGED
(re-claim since observation). The fold's `releases_current` branch (skcoord
`card_store.py` ~1520-1555) releases whenever `card.owner == released_owner
and actual_revision == expected_revision` — i.e. a live worker still holding
the SAME generation it was observed under always matches, and the release
always succeeds (this is step 6 of the executed demo). The CLI
(`coord_release_claim`, `cli/coord.py` ~600-700) adds nothing beyond the same
revision equality (`current_claim_precondition != expected` → refuse).

So the fence protects a card that was re-claimed, and nothing else. The ONLY
live-worker protection in the whole mechanism is the idleness measurement —
the thing Finding 1 breaks. This is not a bug to fix so much as a fact to
state plainly in the design doc: the deadline is load-bearing and alone.

---

## FINDING 3 (CONFIRMED MICRO-BEHAVIOR, LOW REACHABILITY): naive timestamps
## resolve in the reaper host's local timezone

`_parse_ts` → `datetime.fromisoformat(...).timestamp()`. A tz-naive `ts` is
interpreted in the REAPER's local zone. Measured (p2): a naive-UTC timestamp
read on an Asia/Tokyo reaper appears **9.0h older** (Pacific/Kiritimati:
14.0h); on a west-of-UTC reaper it appears in the future, which `evaluate`
refuses (`future-timestamp` — the safe direction, though it also means one
future-dated owner event pins a claim unreclaimable until real time passes it).

Reachability today: low. Every shard event is stamped by `_now_iso()`
(tz-aware UTC, verified skcoord `card_store.py:298`). The naive route needs a
nonstandard writer — note that `append_event` does `event.update(payload)`
AFTER stamping, so any caller passing a `ts=` kwarg silently overrides the
stamp; no current caller does. Worst case it shaves ≤14h off a 48h deadline;
combined with a 1h operator TTL it dominates. Cheap hardening: treat naive
timestamps as UTC explicitly (`replace(tzinfo=timezone.utc)` when
`utcoffset() is None`), or return 0.0 for naive.

Clock skew between hosts (Syncthing lag 10-20s, NTP-scale skew) is negligible
against a ≥1h floor and refuses in the dangerous-to-liveness direction only.

---

## VERIFIED NON-FINDINGS (attacked, held)

- **TTL parsing** (p2, fuzzed): `0.5`, `.5`, `nan`, `NaN`, `inf`, `-inf`,
  `-48`, `1e-300`, `0.999999999999`, `1,5`, `1.5h`, `0x2`, Arabic-Indic
  digits, NBSP-suffixed values → all fall back to the 48h default. `1_0`→10h,
  `1e0`→1h, fullwidth `１`→1h: all ≥ floor, legal. `1e300` → effectively
  never-reclaim (liveness, not safety). Boundary: `idle == ttl` is
  `within-ttl` (safe); floor value 1h is accepted exactly. NOTE: a 1h TTL is
  operator-legal and, under Finding 1, means live workers reaped 61 minutes
  after claim; the floor is only meaningful once Finding 1 is fixed.
- **Mode parser disagreement** (p2, fuzzed incl. case, whitespace, NBSP, NUL,
  fullwidth, Turkish İ): the inline parse in `_expire_idle_claims` and
  `mode_from_env` normalize identically; zero disagreements; no input makes
  one say off and the other enforce.
- **Report mode releasing:** `mode == "report"` returns after logging, before
  the release loop; `dry` returns before it too; the module-scope call sits in
  the non-DRY branch and mode defaults to off with no import. Confirmed by
  reading `scripts/fleet/skfleet-rotate.py` ~4395-4470 and ~4485.
- **No-revision / no-activity claims:** observation with `claim_revision=None`
  or `last_owner_event_at=0.0` is refused by `evaluate` (`no-claim-revision` /
  `no-owner-activity`). A legacy-only or `assign`-derived owner has no
  `_claim_revision` in meta and is therefore never TTL-released.
- **Double-release race:** re-claim between observation and release changes
  the revision → CAS refusal; release-then-reclaim-by-other likewise; the
  `state(cid) != "claimed"` re-read catches same-tick releases by the absence
  path.

## Fold/observe boundary (reasoning only, not a new hazard from this change)

The fold's second-claim rule protects the first owner only while status is
ready/doing/review. Claim A → `move` to backlog/done → claim B silently
transfers fold ownership to B while A's process is live. The TTL mechanism
then measures and releases B, not A — but A was dispossessed by the fold at
B's claim, before this mechanism ran. The TTL path faithfully follows the
fold's (flawed) answer; it does not create the misattribution. No action
required in this change; worth a fold-level card.

## Bottom line

The mechanism's safety story is "a live worker refreshes its own idle clock".
Finding 1 demonstrates, with real store writes end to end, that the two verbs
carrying most long-run worker output (`link` evidence and verdicts) never
refresh it, and Finding 2 confirms nothing else stands behind that clock.
Do not enable `enforce` — and do not trust `report`'s candidate list as a
liveness gate — until `_last_owner_event_at` reads the same merged event set
the fold does.
