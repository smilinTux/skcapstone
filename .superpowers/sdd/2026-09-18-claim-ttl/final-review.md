# Final whole-branch review: feat/claim-ttl-heartbeat

Reviewer: adversarial final gate, 2026-09-18. 16 commits vs origin/main (e432e227).
Worktree: `~/skworld-worktrees/claim-ttl-skcapstone`. Host: noroc2027.

Everything below marked CONFIRMED was produced by running code, either the
shipped test suite, the shipped `observe()` against the live `~/.skcapstone`
store on this host read-only, or a synthetic-card harness that folds the same
directory through both `CardStore.fold()` and `observe()` and prints both
answers. Scripts were written to the session scratchpad and removed.

## Verdict

**NO, not as it stands** - but only because of one finding in the half that is
live immediately. Fix F1 and the branch is mergeable with the TTL mechanism
default off. F2a and F2d must be fixed before any operator sets
`SKFLEET_CLAIM_TTL_MODE=report`, because the report list IS the phase-2 gate
and it is currently both blind to a third of held claims and capable of naming
a card a live worker is holding.

## Test run, done myself

    python3 -m pytest tests/fleet/ tests/test_fleet_lane_health.py -q
    1493 passed, 14 warnings in 44.93s

Matches the ledger's claim. CONFIRMED.

---

# CONFIRMED FINDINGS

## F1. BLOCKER (live immediately). The gateway fix does not restore dispatch. It relabels the outage and suppresses the diagnostic that found it.

Files:
- `src/skcapstone/fleet_lane_health.py:245` - `endpoint = gateway_root(base_url)`, and that normalized value is what goes into `snapshot["endpoint"]`.
- `src/skcapstone/fleet_lane_health.py:338` - `if snapshot.get("endpoint") != endpoint.rstrip("/"): return False, "endpoint-mismatch"`.
- `scripts/fleet/skfleet-rotate.py:6098` - `endpoint=_GATEWAY_ENDPOINT`, the RAW env value, unnormalized.

`acquire_lane_snapshot()` now normalizes the endpoint it probes AND the endpoint
it seals into the snapshot. `lane_health()` is then called with the raw value.
When the raw value carries `/v1`, the two disagree and every lane fails closed
on `endpoint-mismatch` before any health data is even looked at.

Failure scenario, run end to end against a strict mock server that serves
`/health` and `/queue` at the root and 404s `/v1/*`:

    SKFLEET_GATEWAY_URL=http://chiap01:18790/v1
      snapshot.endpoint = http://chiap01:18790   errors = []
      lane_health(endpoint=_GATEWAY_ENDPOINT) = (False, 'endpoint-mismatch')

    SKFLEET_GATEWAY_URL=http://chiap01:18790
      snapshot.endpoint = http://chiap01:18790   errors = []
      lane_health(endpoint=_GATEWAY_ENDPOINT) = (False, 'unknown')  <- proceeds to health

So the `/v1` misconfiguration still blocks every card on the host. Before this
commit it blocked them as `unknown` with `errors: ['health:HTTPError',
'queue:HTTPError']`; after it blocks them as `endpoint-mismatch` with
`errors: []`. **That is strictly worse for diagnosis**: the probe errors were
the signal that identified the three-day, 373-NOOP outage in the first place,
and they are now gone while the outage persists in a new form.

Two secondary defects follow from the same root:
- `docs/fleet/lane-admission-health.md` now asserts "The probe normalizes a path away (`gateway_root()`), so the `/v1` form now works". It does not work. The doc is false.
- `tests/test_fleet_lane_health.py:462 test_v1_suffixed_base_url_still_probes_the_root_endpoints` stops at the snapshot. It asserts the probe URLs, `errors == []`, and a healthy domain, and never calls `lane_health()`/`_admit()` with the raw `/v1` base, which is what the dispatcher actually does. It therefore cannot distinguish "the outage is fixed" from "the outage moved one check to the right". The `_strict_opener` work is genuinely good and does catch the original bug; the test built on it is scoped one layer too shallow.

Fix: `endpoint=gateway_root(_GATEWAY_ENDPOINT)` at `skfleet-rotate.py:6098`, or
normalize once into a separate probe constant. Do NOT normalize
`skfleet-rotate.py:1426` (`env["SKFLEET_GATEWAY_URL"]=_GATEWAY_ENDPOINT`) -
workers need the operator's value, `/v1` included, for chat completions. Then
extend the regression test through `_admit()` and correct the doc sentence.

## F2. BLOCKER for the report path. `observe()` diverges from `CardStore.fold()` on ten of ten probed event shapes, and on 75 of 143 held cards in the live store on this host.

Ran the shipped `observe()` and `CardStore.fold()` over every card in
`~/.skcapstone` (5861 cards) on noroc2027:

    card ids 5861    fold-held 143    replay-held 106    fold errors 0
    cards the replay misses entirely           37
    cards where the reported revision differs  38
    cards the replay over-reports               0

This is a different store from the chi store the ledger validated against, and
the ledger's "22 = 22, zero disagreement" does not generalize. The three
defects already found and fixed were real; there are more than four.

Ten synthetic probes, each one card folded both ways. **All ten diverge.**

### F2a. BLOCKER for functionality. A `claim` with no `claim_revision` gets a fence from the fold and none from the replay, so 36% of held claims can never be collected.

`src/skcapstone/fleet/claim_expiry.py:213` - `revision = e.get("claim_revision") or None`
`skcoord/card_store.py:1576` -              `revision = e.get("claim_revision") or e.get("event_id")`

The fold synthesizes the CAS fence from `event_id`. `observe()` reports `None`,
`evaluate()` (`claim_expiry.py:76`) refuses with `no-claim-revision`, and
`_expire_idle_claims` (`skfleet-rotate.py:4430`) skips it. Such a claim is
permanently uncollectable.

38 of the 106 shared held cards are exactly this shape. Worked example,
`~/.skcapstone/cards/0a3c64b8`, its only claim event:

    lumina@noroc2027.jsonl seq=0 2026-08-21T19:25:28.506356+00:00 claim
      owner=lumina  claim_revision=None  event_id=f89302e599fe42c99cc34bc147fb7737

fold says `('lumina', 'f89302e5...')`; replay says `('lumina', None)`.

This also refutes a premise in the spec and the ledger. The phase-2 baseline
names `72df1b66` as "no claim_revision, so no CAS fence exists (518.1h)" and
lists it among "exactly what an expiry path should collect". A fence does
exist, the fold has it, and the shipped code is the only thing that cannot see
it. As written the mechanism will report that card forever and never take it.

Fix: mirror the fold - `e.get("claim_revision") or e.get("event_id") or None`.

### F2b. SHOULD-FIX. `archive` is treated as FINAL; in the fold only `void` is.

`claim_expiry.py:116` (`_TERMINAL_FINAL = {"void", "archive"}`) and `:190-196` (break).
`skcoord/card_store.py:1690-1692` - `archive` sets `card.archived = True`, `archived_at`, `archived_by`, and **nothing else**. It does not clear the owner and it does not set the `voided` flag, so the `void_terminal_actions` suppression at `card_store.py:1497` never engages for it.

Probe C: `archive` at T1, `claim owner=alice rev=r1` at T2.
fold `('alice','r1')`, replay `None`.

All 37 cards the replay misses in the live store are archived-and-owned. The
direction is safe (under-report, no reclaim) but the CLI report is wrong by 26%
of the held set, and `tests/fleet/test_claim_expiry_observe.py:381
test_archive_alone_is_also_final` actively pins behaviour the fold does not have.

### F2c. SHOULD-FIX. `release_claim` is applied unconditionally; the fold applies a CAS fence.

`claim_expiry.py:215-217` - any `release_claim` clears owner and revision.
`skcoord/card_store.py:1520-1573` - the fold requires `released_owner` to be a non-empty string equal to `card.owner` AND `expected_claim_revision` to equal the live `_claim_revision`, else the release is recorded in `meta["release_conflicts"]` and **the owner stays**. (There is a second accepted form, `releases_conflict`, which retires a matching `claim_conflicts` entry instead.)

Probe B: `claim owner=alice rev=r1`, then `release_claim released_owner=alice expected_claim_revision=STALE`.
fold `('alice','r1')`, replay `None`.
Probe B2: `release_claim` with neither field. fold `('alice','r1')`, replay `None`.

Zero such cards on this store today, so it is latent, not active. But this is
the exact question the brief asked and the answer is that the replay does not
implement the fence.

### F2d. SHOULD-FIX, and the dangerous direction. A second `claim` by a different owner is honoured by the replay and REFUSED by the fold, so the idle clock can be measured against the wrong identity.

`claim_expiry.py:203-213` - `_ACQUIRE` overwrites owner and revision unconditionally.
`skcoord/card_store.py:1578-1594` - when the card already has an owner, the new owner differs, and `card.status` is in {ready, doing, review}, the fold records a `claim_conflict` and **keeps the original owner**.

Probe A: `alice@h1` claims with r1 on Sep 1; `bob@h2` claims with r2 on Sep 2, no release between them.
fold `('alice','r1')`, replay `('bob','r2')`.

Failure scenario: alice is a live worker mid-run, writing move/evidence events
every few minutes. Something re-claims as bob and then does nothing. The replay
names bob as owner and measures `last_owner_event_at` from bob's single claim
event, so after 48 hours the card appears as `RECLAIM ... jarvis-shaped owner
idle=48.0h` in report mode - **a card a live worker is holding, in the phase-2
would-reclaim list, which is exactly what spec success criterion 6 forbids and
what the ledger designates as blocking enforce.**

In enforce mode there is no theft: `coord release-claim` calls
`current_claim_precondition(home, task_id, owner)` and raises
`claim revision conflict` (`src/skcapstone/cli/coord.py:660-670`), so the CAS
fence holds and the reaper logs `CLAIM_TTL_FAILED`. Confirmed by reading the CLI.
Two residual effects worth knowing: the fold's `releases_conflict` branch can
accept such a release and silently retire the `claim_conflicts` record, and the
reaper's confirmation re-read would then log `CLAIM_TTL_INEFFECTIVE ... needs
repair`, a false alarm about store divergence.

Structural note: the replay **cannot** reproduce this rule as written, because
the fold's condition is status-dependent and `observe()` never tracks status -
it ignores `move` and `reopen` entirely. Reproducing the fold requires tracking
`card.status` or calling the fold for ownership and using the event log only
for the owner-idle clock. The ledger's Task-2 ruling rejected importing
`CardStore` on the grounds that it "would buy nothing"; F2a through F2f are what
it bought.

### F2e. SHOULD-FIX. The sort key still differs from the fold's in two ways.

`claim_expiry.py:183` - `key=lambda e: (_parse_ts(e.get("ts")), e.get("seq") or 0)`
`skcoord/card_store.py:1427` and `:1484` - `key=lambda e: (e.get("ts",""), e.get("writer",""), e.get("seq",0))`

Two differences: the fold compares `ts` as a STRING, and its tiebreak is
`writer` before `seq`.

Probe G, identical `ts` across two shards: `alice@h1` claim seq=5, `zrel@h2`
release seq=0, both at `2026-09-01T00:00:00+00:00`. Fold orders by writer
("alice" < "zrel") so claim then release, FREE. Replay orders by seq so release
then claim, HELD with `('alice','r1')`. CONFIRMED.

Probe E, a non-ISO `ts` on the release (`"ts": "not-a-date"`). `_parse_ts`
(`claim_expiry.py:100-106`) maps it to 0.0 and it sorts FIRST; the fold's string
sort puts `"not-a-date"` after every `"2026-..."` and it sorts LAST. Full
inversion: fold FREE, replay HELD. CONFIRMED.

Both over-report. In enforce mode the `state()` re-read
(`skfleet-rotate.py:4427`) catches it and logs `CLAIM_TTL_SKIPPED`, so no bad
release. The report lies.

### F2f. SHOULD-FIX. `observe()` never reads `core.json`, which is where the fold gets its initial ownership and its existence check.

`claim_expiry.py:132-148` reads `cards/*/events/` only.
`skcoord/card_store.py:1450-1479` - `fold()` returns `None` when `core.json` is absent, RAISES when it is malformed, and otherwise seeds `card.owner = core["initial_owner"]` with `card.meta["_claim_revision"] = core["initial_claim_revision"]` (and raises if an owner has no revision).

Probe H: core with `initial_owner=alice, initial_claim_revision=r0` and only a
`note` event. fold `('alice','r0')`, replay nothing. CONFIRMED.

The inverse is the over-report: a card directory with an events dir and no
valid `core.json` is invisible to the fold and reported by `observe()`. That is
a release attempt against a card the store says does not exist.

### F2g. SHOULD-FIX. `observe()` reads files the fold does not, and silently tolerates corruption the fold rejects.

`claim_expiry.py:156-171` vs `skcoord/card_store.py:1379-1425`.

- The fold reads only `*.jsonl`. `observe()` reads every file in the directory. Probe F: the release lives in `z@h2.jsonl.tmp`. fold `('alice','r1')`, replay `None`. CONFIRMED. (No non-`.jsonl` files exist in the live store today; checked, count 0.)
- The fold RAISES `CardStore event source ... is malformed` on a corrupt JSON line; `observe()` skips it. Probe I: fold raises `ValueError`, replay returns `('alice','r1')`. CONFIRMED. `tests/fleet/test_claim_expiry_observe.py:117 test_observe_survives_a_corrupt_line` pins that divergence as intended behaviour.
- The fold verifies the per-file `prev_hash` chain and raises on a break. `observe()` does not look at `prev_hash` at all, so a tampered or truncated shard folds cleanly in the replay and hard-fails in the store.
- `observe()` decodes with `errors="ignore"` (`claim_expiry.py:158`); the fold raises on `UnicodeError`.
- `except OSError: continue` at `claim_expiry.py:159-160` silently drops one event SHARD mid-card and keeps going with partial history. If the dropped shard held the release, a free card looks held. This is the "reports success, does nothing" class the repo has been bitten by: per-card read failure should surface, not vanish.
- The fold refuses a symlinked card entry as unsafe (`card_store.py:1712-1713`); `observe()` follows it.

### F2h. SHOULD-FIX. `_parse_ts` resolves a naive timestamp in the HOST's local timezone.

`claim_expiry.py:104` - `datetime.fromisoformat(...).timestamp()`. On a naive
value there is no tzinfo, so `.timestamp()` applies the host's local offset.

Probe J: `"ts": "2026-09-01T00:00:00"` yields 1788235200 on this host instead of
1788220800 - a 4-hour error (EDT), and a different error on a host in another
zone. CONFIRMED. Ownership still agreed in this probe; what breaks is
`last_owner_event_at`, hence `idle_seconds`, hence the deadline.

No naive and no non-UTC-offset timestamps in the sampled live store (checked
~511 events across 400 files: 0 naive, 0 non-UTC), so this is latent. Fix is one
line: treat a naive parse as UTC.

## F3. SHOULD-FIX. `SKFLEET_CLAIM_TTL_H=nan` makes every held claim reclaimable, and there is no floor on the TTL.

`src/skcapstone/fleet/claim_expiry.py:51-59`. `float("nan")` parses, `hours <= 0`
is False for nan, so `ttl_seconds = nan`. In `evaluate()` at `:82`,
`idle <= nan` is False, so control falls to the `else` and every observation
becomes `idle-beyond-ttl`, `reclaimable=True`. CONFIRMED:

    'nan'    ttl=nan     1h-idle claim reclaimable=True  reason=idle-beyond-ttl
    'inf'    ttl=inf     1h-idle claim reclaimable=False reason=within-ttl
    '0.0001' ttl=0.36s   1h-idle claim reclaimable=True  reason=idle-beyond-ttl
    '48'     ttl=172800  1h-idle claim reclaimable=False reason=within-ttl

`inf` and `-inf` and overflow are safe; `nan` is the one that inverts.
`0.0001` shows the second half: any positive float is accepted, so a
fat-fingered `.5` gives a 30-minute deadline and reclaims live work on the next
cycle. Given the spec's whole argument is that the failure is asymmetric and a
short TTL is "strictly worse than the bug", the config reader should reject
non-finite values and refuse anything below a floor (1h would do).

## F4. SHOULD-FIX. Spec deviation: the TTL release is not distinguishable from the absence path's, and records no outcome.

The spec requires "an ordinary `release_claim` event carrying
`reason: "lease-expired"` so the action is auditable and distinguishable from a
worker's own release".

`scripts/fleet/skfleet-rotate.py:4345-4350` sends
`--abandon-reason error`, byte-identical to the absence path at
`skfleet-rotate.py:4283-4286`. In the event log the two paths are
indistinguishable; the only distinguishing record is `CLAIM_TTL_RECLAIMED` in
the host's rotate log.

Separately: the absence path calls `_record_reap_outcome()` before releasing
(`skfleet-rotate.py:4279-4281`), whose own comment is "never release without a
durable outcome", writing a `WORKER_DIED` verdict and a `worker_died` link. The
TTL path writes neither. Whether that matters depends on whether the BLOCKED
backoff and provisional-review logic need an outcome to avoid re-dispatch
churn; it is at minimum a deliberate asymmetry that is not written down.

Not dangerous while the mechanism is off. It is the audit property the spec
asked for, and it is missing.

## F5. SHOULD-FIX. Nothing computes success criterion 6, the two paths have no arbitration, and report mode has a built-in false positive.

Asked directly: what happens when the absence path and the TTL path run in the
same cycle and disagree?

- **Both releasing the same generation: cannot happen.** `_expire_idle_claims()` runs after `reap_dead_claims()` (`skfleet-rotate.py:4473-4474`), calls `observe()` at call time (after the absence path finished), and re-reads lifecycle state with the caches popped immediately before each release (`_claim_ttl_fresh_state`, `:4353-4362`). Verified by test and by reading. CLEAN.
- **Opposite conclusions: unarbitrated, and the TTL path wins.** The absence path can refuse because a host reports the owner PRESENT, which is positive proof of life, and the TTL path then releases anyway because the owner wrote no events. The spec says that is intentional ("dead or making no progress"). Fine as policy. What is missing is that report mode (`skfleet-rotate.py:4406-4413`) logs card, owner, revision and idle hours and **no liveness annotation at all**, so criterion 6 ("the would-reclaim list contains no owner that is live on any host") has to be evaluated by hand against a separate source. For a gate that is the sole thing standing between report and enforce on a 5-host production cluster, the gate should be computed, or at least the list should carry what `live_report_health()` already knows.
- **Built-in false positive.** `lifecycle_state()` (`skfleet-rotate.py:2403-2415`) returns `"claimed"` only when the fold has an owner AND status is in {ready, doing, review}. It returns `"void"` for anything archived. On this store 16 of the 106 held cards the CLI reports have status `backlog`, and all 37 archived-and-owned ones are `"void"`. Every one of those will be printed as `RECLAIM` by `skfleet-claim-expiry` and then permanently skipped as `CLAIM_TTL_SKIPPED|...|no longer claimed` by the reaper. The report and the actor disagree by construction, and the skip message blames the absence path for something that never happened.

## F6. NIT. One em dash, which is a hard repo rule violation.

`.superpowers/sdd/2026-09-18-claim-ttl/progress.md:1`

    # SDD ledger - plan: docs/superpowers/plans/2026-09-18-claim-ttl.md

The only other hit in the diff is `docs/superpowers/plans/2026-09-18-claim-ttl.md:311`,
which is the grep pattern `"-\|-"` itself and has to contain the characters to
be the check. Legitimate. No em or en dash anywhere in `src/`, `scripts/`, or
`tests/` on this branch.

## F7. NIT. `idle_seconds` is nonsense on a refused row.

`claim_expiry.py:73` - `idle = now - float(o.last_owner_event_at or 0.0)`. When
nothing was found, `last_owner_event_at` is 0.0 and `idle` becomes the whole
Unix epoch, so `claim_expiry_cli` prints `hold  <card>  idle=496761.2h`. Also
the check order at `:76-81` reports `no-claim-revision` in preference to
`no-owner-activity` when both hold, so the report names the less informative
refusal.

---

# AREAS THAT ARE CLEAN

- **`reap_dead_claims()` is genuinely unmodified.** Extracted the function from both `origin/main` and HEAD and diffed: identical, 157 lines, no output. CONFIRMED. (`test_reap_dead_claims_is_untouched_by_this_change` only asserts the function does not NAME the new helpers, which is much weaker than what I verified, but reality is fine.)
- **The new path consults none of the absence machinery.** No reference to `_parse_worker_owner`, `live_report_health`, `REAP_QUORUM` or `CLAIM_GRACE` in any of the three helpers. The guard test at `tests/fleet/test_claim_expiry_reaper.py:352` is NOT vacuous: `_helper_source()` asserts `{node.name for node in nodes} == set(HELPERS)` before joining, and `ast.unparse` strips comments, so the module-level comment at `skfleet-rotate.py:4325` that mentions `_parse_worker_owner` cannot defeat it. One gap worth noting: the guard inspects only the three helpers, and `_claim_ttl_fresh_state` reaches `lifecycle_state` → `_authoritative_card_state` → `_authoritative_card_snapshot`; I read that chain and it does not consult quorum or report health either.
- **It cannot release in dry-run, in `off`, or in `report`.** `_expire_idle_claims()` is called only in the `else` branch of `if DRY:` (`skfleet-rotate.py:4470-4474`), so DRY is structurally unreachable there, and the internal `dry` check is redundant defense. `off` returns at `:4392` before `observe()` touches disk. `report` returns 0 at `:4413`. Unknown mode values fall to `off` (`claim_expiry.py:64`). All four exercised by real tests, not mock assertions.
- **The owner predicate is sound on the question asked.** `claim_expiry.py:223` uses equality, not substring, so `jarvis` does not collide with `jarvis-2` or `pi-codex-jarvis-x`. A different actor writing constantly (`mero`, `coord`, `jarvis`) does NOT reset the clock. The one soft spot is that `e.get("owner") == owner` counts a third party's event that merely NAMES the owner (a `coord`-written `assign owner=jarvis`) as owner activity, which inflates freshness and therefore only ever under-reclaims. Acceptable.
- **Every path into `evaluate()` with `last_owner_event_at == 0.0` is refused.** The acquire event that sets the owner carries `owner == owner`, so the only way to reach 0.0 is an unparseable `ts` on every one of the owner's events, and `:78` refuses that as `no-owner-activity`. Traced all callers (`claim_expiry_cli.py:31`, `skfleet-rotate.py:4398`).
- **`gateway_root()` itself is correct for its stated job and safe for its other consumer.** `active_gateway_revision()` (`fleet_lane_health.py:77-112`) reads only `parsed.hostname` and `parsed.port`, so the discarded path is genuinely irrelevant to it, and the docstring's claim checks out. `skfleet-rotate.py:1426` correctly passes the RAW value to workers, who need `/v1`. Edge cases behave: `""` → `""`, `"not-a-url"` → unchanged, `"localhost:18790/"` → `"localhost:18790"`, `"http://h:1/v1?x=1"` → `"http://h:1"`. The bug is not in `gateway_root()`, it is that only one of its two consumers was updated (F1).
- **Console script target is real.** `skfleet-claim-expiry = "skcapstone.fleet.claim_expiry_cli:main"`; `importlib.import_module` resolves it and `main` is callable. CONFIRMED with `PYTHONPATH=src`. The packaging test is a genuine test, not a tautology.
- **No `Co-Authored-By` footer in any of the 16 commits.** The only matches in the diff are plan/report prose instructing agents never to add one. CONFIRMED via `git log --format='%H %s%n%b'`.
- **The reaper tests are well built** and largely not of the "cannot fail" class. `test_off_never_reads_the_store` genuinely bites (the import is at function top, before the mode check, and monkeypatching the module attribute is observed because the import is per-call). `_claimed_then_open()` is per-card rather than per-call on purpose, which is the right call. `test_a_zero_exit_is_not_proof_the_claim_moved` and `test_enforce_skips_a_card_the_absence_path_already_released` both assert on behaviour, not on mocks.

---

# WHAT MUST CHANGE BEFORE MERGE

1. **F1.** `endpoint=gateway_root(_GATEWAY_ENDPOINT)` at `skfleet-rotate.py:6098`. Extend `test_v1_suffixed_base_url_still_probes_the_root_endpoints` through `_admit()` so it asserts a lane is ADMITTED, not merely probed. Correct the false sentence in `docs/fleet/lane-admission-health.md`.

# WHAT MUST CHANGE BEFORE `SKFLEET_CLAIM_TTL_MODE` IS EVER SET

2. **F2a.** `revision = e.get("claim_revision") or e.get("event_id") or None`. Without it the mechanism silently declines a third of its target population, including the spec's own worked example, and the operator will read that as "the TTL is working, those cards are just fenceless".
3. **F2d.** Either track `card.status` in the replay and reproduce the fold's claim-conflict refusal, or take ownership from `CardStore.fold()` and use the event log only for the owner-idle clock. The second is less code and removes F2b, F2c, F2f and F2g at the same time. Until one of them lands, the phase-2 would-reclaim list can name a card a live worker is holding, and criterion 6 cannot be checked.
4. **F3.** Reject non-finite `SKFLEET_CLAIM_TTL_H` and impose a floor.
5. **F5.** Put liveness in the report line, or accept in writing that criterion 6 is a manual cross-reference. And reconcile `lifecycle_state`'s `{ready, doing, review}` gate with what the CLI prints, or the first report-mode run arrives with dozens of rows that can never be acted on.

The remaining should-fix items (F2b, F2c, F2e, F2f, F2g, F2h, F4, F6, F7) are
real but none of them can cause a bad release while the mode is `off`, and the
CAS fence plus the `state()` re-read means even the over-reporting ones fail
safe in `enforce`. They are report-accuracy and audit-trail defects, and the
report is the thing the rollout is about to make decisions from.
