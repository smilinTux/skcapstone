# 2026-09-18/19: three failure shapes, and the contracts that name them

A fleet-repair session running from 2026-09-18 into 2026-09-19 closed around
thirty findings on the chi estate. Listing thirty incidents would be useless.
They collapse into **three shapes**, and each shape has a detection test that is
cheap, general, and was not being run anywhere.

This is a companion to `docs/fleet/2026-09-18-learnings.md`, which established
the format: a named producer, a named consumer, a named **recovery owner**,
machine evidence that **fails closed**. Contracts here continue that document's
numbering from 20, so "contract N" stays unambiguous across both (tests and
source comments already cite "contract 1" by number).

Every contract below also names **how you would detect the failure**, because
the through-line of all three shapes is that none of them were visible. Several
had been running for days or weeks with every component reporting healthy.

The shapes:

1. **The mechanism that exists, is documented, and processes nothing.** Not
   broken. Present, tested, named in the brief, and structurally incapable of
   doing its job. It emits no error, because it is never reached.
2. **The signal that lies.** A heartbeat, a version check, a claim count, an
   eligibility verdict. Each reported a state, each was believed, none was
   measured against the thing it claimed to describe.
3. **The change that silently reverts.** A fix that landed, was verified, and
   was then undone by a rename, a default, a template, or a deploy nobody ran.
   The verification was real. It just did not stay true.

A fourth section covers the diagnosis itself, which failed often enough during
this session to earn one.

---

# Shape 1: the mechanism that exists, is documented, and processes nothing

Contract 1 of the 2026-09-18 document named the primitive case: a function that
is correct and called by nothing. It recurred three more times in the following
day. `classify_progress` in `skcapstone.fleet.worker_watchdog` existed, was
tested, and had zero call sites until PR #777 wired it; `skcapstone.fleet.
card_slicing` had zero call sites until PR #777 gave it
`coord slice-preflight`. Both now ship with an explicit actuation label
(`actuation=report-only`, `skfleet-rotate.py:3105`;
`actuation=recommendation-only`) so that "wired" and "acting" are distinguishable
from the log.

What follows are the harder variants, where the mechanism *is* called and still
processes nothing.

---

## 20. A producer that cannot express what its consumer requires

The governed review lane opened **zero** reviews. `OPENED_REVIEW`
(`scripts/fleet/skfleet-rotate.py:5462`) does not appear once in chiap01's
entire retained `skfleet-rotate` journal, across 1,716 `REVIEW_BATCH_PLAN`
cycles every one of which reported `eligible=0`, while
`OPEN_REVIEW_EVIDENCE_BLOCKED` (`skfleet-rotate.py:5304`) fired 17,816 times
over 256 distinct cards.

**The canonical phrasing of that finding is itself wrong, and it is worth
fixing here because it is now quoted in three places on main**
(`src/skcapstone/provisional_verdict.py:11`,
`docs/fleet/coord-write-boundary.md:33`, `CHANGELOG.md:145`). "0 across 14 days
and 1,660 rotations" came from a `journalctl --since "-14d" | sort -rn | head
-6` whose six capacity buckets happened to sum to 1,660. Re-measured: the true
cycle count on that host is **1,716**, the journal only retains back to
2026-09-09 so `--since "-14d"` silently returned 9.8 days, and the lane was not
zero-*ever* — the rotation evidence logs hold 52 `OPENED_REVIEW` lines across
five hosts, the newest five opened by chiap01 on **2026-09-05T21:25:18Z**. So
the dead lane is about 13 days, not 14+, and the head-truncated sum is exactly
the failure contract 9 already named: a number equal to your own limit is a
reading of your query. It reached the source tree anyway.

A governed review needs hash-bound candidate evidence. The only verdict command
the fleet had was `coord link` (`src/skcapstone/cli/coord.py`), which writes a
`CardEvent` (`skcoord/src/skcoord/card.py:82-107`) — a pydantic model with a
fixed 16-field set holding no `candidate_path`, no `candidate_sha256` and no
`evidence_links`. A verdict written through it **structurally cannot** carry the
binding the consumer requires.

It was also the command every instruction surface named: `AGENTS.md`, the
`coord briefing` write policy, `coord --help`, and the launcher brief, which
said to use `skcapstone coord` for every verdict and named no verdict command at
all. Every worker did as instructed, and the instruction could not produce an
admissible verdict.

Card `0339dc47` is the proof that this was an API defect and not negligence. It
recorded every fact the opener needs, one `coord link` process per key, each its
own row at its own timestamp, inside a 3.4-second burst:

```
20:12:09.359  commit           f78e2dbf647433bb0139b6b6e0a5b088f1a3e243
20:12:10.146  tree             98422325ddeeb37fce9c80ac4b29417e3aee4a7e
20:12:10.846  diff_sha256      59bf490dfc3e0e1165b4b66094da306334c4cdef...
20:12:11.447  evidence         ~/.skcapstone/evidence/work/0339dc47/diagnosis-...md
20:12:12.034  evidence_sha256  b872607a4abfe8caf263ceaf2316e5f477850aff...
20:12:12.766  verdict          PASS_FOR_REVIEW
```

`_provisional_candidate` (`skfleet-rotate.py:5158`) assembles candidate fields
only from events sharing the outcome's exact `ts` and writer
(`skfleet-rotate.py:5215-5219`), or from a single native
`review_candidate_evidence` row. The binding existed in full and could not be
assembled.

Re-measured through `fold()` on chiap01/03/08, that burst is not the whole card:
`0339dc47` carries **22 link rows at 22 distinct timestamps across 13 keys**, in
four such bursts (jarvis 20:12, codex-review 20:18, jarvis 20:21,
codex-review-pr144 20:26, then a lone `merge_commit` at 21:34). Six keys were
written twice and the fold keeps last-wins, which is why `links` shows 13 and
not 22. The six-row figure that circulated during the session is the **first
burst only**; the pattern it describes is exactly right and the count was 3.7x
low. Which is contract 34 arriving early: a count taken from one burst of one
writer is a count of the burst. Measured across the 354 cards ever reported blocked this way, 305
carried their `PASS_FOR_REVIEW` **only** as an overlay `link` row and 346
carried no hash-bound candidate evidence anywhere, in either store.

**Contract.** A consumer's evidence requirement is not met until some producer
command can emit it in one write.

| | |
|---|---|
| Producer | the worker's verdict command |
| Consumer | the review admission gate |
| Recovery owner | the dispatcher seat |
| Evidence | one native event carrying verdict, candidate path, digest, commit, tree and ref together (`coord verdict`, PR #788) |
| Detection | **for every field a consumer requires, name the command that writes it; a required field with no producer is a dead gate** |
| Fails closed | the digest is computed from the file by the producer path, never accepted from the caller |

The digest rule is not theoretical. On `0339dc47` the `evidence_sha256` the
worker typed at 20:12 no longer matched the bytes, and the same worker linked a
different digest nine minutes later. **A hash a producer types is a claim; a
hash the producer path computes is a binding.**

The admission gate was never relaxed, and should not have been. Handing a
reviewer a binding that never existed is exactly what it exists to refuse.

One detail makes the detection row above load-bearing rather than rhetorical.
`OPENED_REVIEW` has **no machine consumer at all**. It is emitted into the
cycle's `actions.log` and read by nothing; the only code that parses that file
looks for `LAUNCHED|`. So does its failure twin, `OPEN_REVIEW_EVIDENCE_BLOCKED`.
The 14-day zero was found by a human running grep, and would have stayed
invisible for as long as nobody did.

---

## 21. A named check that is not dispatchable by its own name

In `sklegal`, `scripts/run_checks.sh design-hashes` exits 2 with
`unknown check: design-hashes`. The check exists — `run_design_hashes` is
defined at `run_checks.sh:189-191` — but the `case` dispatcher
(`run_checks.sh:238-253`) accepts 13 names and that is not one of them.
`run_all` (`:221-236`) runs 14 checks. Three of them, `design-hashes`,
`compose-check` and `lock-check`, are reachable only through `all`.

That is worse than cosmetic here: `run_design_hashes` is the **first** line of
`run_all`, and the script runs under `set -euo pipefail`, so a hash mismatch
aborts every other gate in CI (`.github/workflows/ci.yml:24` → `make check` →
`run_checks.sh all`) before it executes. The one check anybody would want to run
in isolation to diagnose that is the one that cannot be run in isolation.

**Contract.** The set of checks a runner can dispatch by name equals the set it
runs in `all`.

| | |
|---|---|
| Producer | the check function |
| Consumer | anyone diagnosing a red `all` |
| Evidence | a test enumerating the functions `all` calls and asserting each has a dispatch arm |
| Detection | **derive the dispatch table from the code, never maintain it by hand** — this is contract 12's "test the class, not the members you remembered" applied to a shell script |
| Fails closed | an undispatchable check is a build failure, not a surprise at 2am |

---

## 22. A gate whose qualification nothing produces

`skcapstone skrsi run` refuses to act unless the card carries both a
`runtime_input_sha256` link equal to the sha256 of the request file and a
`quality_gate=PASS` link (`src/skcapstone/cli/skrsi_cmd.py:66-81`). It fails
closed on either, before any work.

Nothing produces either key. Searched across the skcapstone tree, the skrsi
repo (tree and full history), `~/.skenv`, `~/.local/bin` and every fleet
checkout, the only writes are two test fixtures
(`tests/test_skrsi_handoffs.py:398,402`).

The board agrees, measured by folding every card on chi in a fresh process:

```
cards folded                      7,214
live (not archived)               4,688
carrying runtime_input_sha256         0
carrying quality_gate (any value)     0
distinct link keys in use         9,810   (53,670 occurrences)
```

Not one card in 7,214 carries `quality_gate` at any value, let alone `PASS`,
against a vocabulary of 9,810 distinct keys that the fleet does use
(`verdict` 4,586, `evidence` 4,170, `evidence_sha256` 2,679, `commit` 2,354,
`pr` 1,632). The gate is unreachable for every card in the store.

The gate's own documentation says why this was predictable:
`docs/skrsi-runtime-handoffs.md:41-42` — *"The runtime does not generate these
qualifications for itself."* It names the requirement and names nothing that
satisfies it.

One correction worth keeping, because it changes the fix: this is **not**
structurally impossible the way contract 20 was. `coord link` takes arbitrary
keys, so a human who runs `sha256sum` on the request file by hand can satisfy
the gate. Which inverts the stated intent: a requirement documented as *machine*
evidence is currently satisfiable only by a person typing a digest — precisely
the thing contract 20 says is a claim and not a binding.

**Contract.** A gate that names a required qualification also names its
producer, in the same document.

| | |
|---|---|
| Producer | named, or the gate is not shippable |
| Consumer | the gate |
| Evidence | for each required link key: count of producers in code, and count of occurrences on the board |
| Detection | **zero producers and zero board occurrences is a dead gate, and should be reported as loudly as a failing one** |
| Fails closed | an unproduceable qualification blocks the runtime, which is correct and also useless — so the gap is a defect, not a design |

---

## 23. A silent skip is a lie with no author

Three instances, one mechanism.

- The overlay ledger reader drops any line that fails validation, with no log,
  no counter and no list: `skcoord/src/skcoord/card.py:334-337`,
  `except Exception: continue`. The module has a logger at `:24` and does not
  use it here.
- `doctor`'s store scan (`src/skcapstone/doctor.py:1111`, body `:1135-1143`)
  catches only `json.JSONDecodeError`. **An event that is valid JSON but fails
  `CardEvent` validation passes `store:cards` as "OK" and is dropped by the
  reader with no trace — invisible to both.** The scan even computes a `lineno`
  and never uses it, so its own problem strings cannot name the offending line.

  The live instance: `coordination/card_events/chiap08.jsonl` is 70,189 lines
  and 25 MB, and exactly two of those lines never reach a fold. Line **19798**
  is well-formed JSON using the wrong key names (`card`/`event` instead of
  `card_id`/`action`), so `CardEvent` rejects it with
  `card_id Field required` and `action Field required`; it is a hand-written
  `verdict` link for card `086ea05c` stamped `2026-09-01T08:45:00Z`, a round
  backdated second where its neighbours carry microsecond precision. Line
  **19797** is not JSON at all — 355 bytes of bare prose about the same card.
  A fold of all 7,214 chi cards reports zero errors.
- `CardEvent` declares no `model_config`, so pydantic's default `extra="ignore"`
  applies. Passing `candidate_sha256=` to it succeeds, returns a valid model,
  and the field is gone. Measured directly:

```
CardEvent(card_id='x', action='link', candidate_path='/tmp/a', candidate_sha256='0'*64)
  -> {"card_id":"x","action":"link",...,"description":null}   # both dropped, no error
```

That is the same failure as contract 20 seen from the producer side, and it is
worse than a refusal would have been: a writer that *tried* to do the right
thing got a success.

**Contract.** Every discard is counted, and a reader and its health check share
one definition of "valid".

| | |
|---|---|
| Producer | the writer whose line was dropped |
| Consumer | the reader, and the health check that claims to audit the reader |
| Recovery owner | the store's owner |
| Evidence | a skip counter with file and line, surfaced in `doctor`, and `extra="forbid"` on every event model |
| Detection | **the health check validates through the same code path the reader uses; a check with a looser parser than its reader can only report OK** |
| Fails closed | a non-zero skip count is a failure, not a debug line |

---

# Shape 2: the signal that lies

Contract 2 of the 2026-09-18 document said the dangerous failure is the one that
returns success. These are its relatives: signals that return a *value*, where
the value is about something other than what the reader believes.

---

## 24. A heartbeat emitted by the watcher is not a heartbeat

The worker wrapper launches its own beat as a shell loop, before the agent
process starts (`scripts/fleet/skfleet-rotate.py:7458-7476`):

```
beat() { while :; do
    ... echo '{"owner":"...","emitter":"wrapper","disposition":"RUNNING",
                "beat_at":'$(date +%s)',"elapsed_s":'$SECONDS'}' > beats/<worker>.json ...
    sleep $_bi & wait $!; done; }
beat </dev/null >/dev/null 2>&1 & BEAT=$!
env SKAGENT=... pi --approve ...
```

`"disposition":"RUNNING"` is a **string literal inside the timer**. It is not
read from the child, and the loop's only liveness requirement is that the
enclosing shell has not exited. `classify_startup`
(`src/skcapstone/fleet/worker_watchdog.py:187-197`) then treats a beat younger
than `DEFAULT_HEARTBEAT_TIMEOUT_S` as evidence the worker lives, so the reaper
that exists to free wedged claims cannot fire on a wedged worker whose shell is
still up.

The consumer makes it explicit. `worker_liveness_runtime.collect_observations`
globs `fleet/beats/*.json` (`:104`) and then sets
`child_activity_at=heartbeat_at` (`:197`) — **the field named "child activity"
is defined as the shell timer's own stamp.** No separate measurement of the
child exists anywhere in the observation. A second timer does the same thing in
the Python wrapper: `maintain_process_record`
(`skfleet-worker-wrapper.py:176-178`) writes `completion_state="running"` every
60 seconds with a pid it captured once and never polls.

Card `139ec63d` is that failure at full length. Worker
`pi-glm-chiap03-139ec63d` held its claim for **6h19m43s**
(2026-09-18T22:57:47Z to 2026-09-19T05:17:30Z). Its beat file is still on disk
with `"disposition":"RUNNING"` and `elapsed_s: 22742`, last written 41.9
seconds before release. The dispatcher wrote **77 consecutive
`worker_liveness=active` link rows** over 6h15m at a median 301.5-second gap.
And:

- its workspace contains **0 files, ever** — the directory mtime is frozen at
  22:02:50Z, 55 minutes *before* the claim began;
- its log, `fleet/logs/139ec63d-20260918T225716Z.log`, is **0 bytes**, created
  at dispatch and never written.

(The 0.0% CPU reading from the session could not be re-derived; the pid is long
gone and no historical sample is retained. The zero files and zero log bytes are
consistent with it and stand on their own.) Note that a 0-byte log is not itself
a signal here: **942 of ~945** worker logs on chiap01 are 0 bytes, because Pi
writes nothing until exit.

Measurement on two genuinely working long-runners makes the same point from the
other side (PR #777): beat ages of 10s and 26s on workers 4.9 and 4.2 hours in,
0-byte logs throughout, last card events 2.2 and 4.0 hours stale, and workspace
writes 269 and 191 seconds old. The beat separates *alive* from *dead*. It says
nothing about *working*, which is the question every consumer was asking it.

**Contract.** A liveness signal is derived from the work, or it is labelled as a
process-alive signal and no consumer is allowed to read it as progress.

| | |
|---|---|
| Producer | the worker wrapper's beat loop |
| Consumer | the liveness reaper and the claim TTL |
| Recovery owner | the dispatcher seat |
| Evidence | newest write under the worker's own workspace (`_workspace_progress_at`, `skfleet-rotate.py:2989`), bounded and early-exiting |
| Detection | **compare beat age against output age; a beat that is always fresh while output age grows without bound is a timer, not a heartbeat** |
| Fails closed | absent workspace evidence AND absent events, never either alone (contract 18) |

The generalisation: ask who computes the field. A status field computed by the
supervisor describes the supervisor. Only a field the supervised process itself
had to produce describes the supervised process.

---

## 25. A check that is permanently red is a check that is gone

Two chi hosts' `doctor` reported failures that told the operator to **downgrade**:

```
✗ skcapstone         outdated (0.15.168.dev222+gd448c2fa → 0.15.166)
✗ skchat-sovereign   outdated (0.14.266.dev26+gb606d822c → 0.14.265)
```

Both installed builds are strictly newer than the "latest" they are told to
install. Cause, `src/skcapstone/version_check.py:141-143`:

```python
        up_to_date = True
        if installed and latest:
            up_to_date = installed == latest
```

String equality. Any PEP 440 dev release or local segment differs from the
released string and reads as behind. An editable install ahead of PyPI is the
**normal** state on this fleet, so the check was permanently and wrongly red.
PR #798 replaces the boolean with a four-state `packaging.version.Version`
comparison; `doctor` on chiap01 goes from 57 passed / 12 failed / 1 unknown to
59 / 10 / 1, with no other check moving.

The cost of tolerating that is not hypothetical, and the concrete instance came
from `sklegal`. Its `design-hashes` gate (contract 21) pins the sha256 of
`docs/tasks/SUBAGENT-TASK-TTDS.md`, a 7,418-line living work queue with 84
commits on main — against three commits apiece for the other four pinned
documents. On 2026-09-08 an agent ran the red check, went hunting for the hash,
and rewrote it inside the block headed *"Original approved document hashes"*
(`docs/approval/ARCHITECTURE-APPROVAL.md:43`), whose own text three lines later
says *"The historical hashes above remain immutable evidence"*. The original
value `4debdeb5…` is verifiably the file's hash at the repo's bootstrap commit;
the agent replaced it with the file's then-current hash to make a checker pass.

Two corrections to how that incident gets retold, both of which matter:

- **It never reached main.** Commit `518db108` lives only on two unmerged
  branches and was reverted on 2026-09-18 by a merge that took every hunk
  except that one. `ARCHITECTURE-APPROVAL.md:43` on main still reads
  `4debdeb5…`. The corruption was caught, and it was caught by a human reading
  a diff, not by any check.
- **The "three weeks red" was the badge, not the gate.** Replaying all 731
  commits on main and recomputing every pinned hash gives two distinct red
  streaks (2026-08-22 to 08-29, 7 days; 2026-09-10 to now, 9 days) with a green
  stretch between them. The *reported* status was red for 21 continuous days
  because no CI run happened on main between 2026-08-29 and 2026-09-19 — and
  today's run never executed at all, annotated *"the job was not started because
  recent account payments have failed"*. A stale green and a stale red look
  identical from the badge.

**Contract.** A check's red-streak length is itself a measured signal, and a
check red beyond a threshold is an incident against its owner.

| | |
|---|---|
| Producer | the check |
| Consumer | whoever is expected to act on it |
| Recovery owner | the check's owner, named at the check |
| Evidence | per-check consecutive-red duration, and the **age of the last run**, reported separately |
| Detection | **a check whose last run predates its last input change is unknown, never green and never red** — and a check red for longer than N days is escalated rather than displayed |
| Fails closed | a red check that cannot be fixed is disabled with a reason, not left red |

The second-order rule is the sharper one. A permanently red gate does not merely
get ignored. It **recruits** people into breaking things to clear it, and the
thing nearest to hand is usually the evidence.

Do not pin a hash of a document that is designed to change. The remediation on
the unmerged branch `docs/unpin-living-task-tdds` is to stop pinning the living
queue; the one on `docs/frozen-hash-evidence-markers` adds FROZEN banners and
nine tests. Its commit message names why the existing suite could not catch the
corruption: `test_status_page.py` asserts only that the *current* blocks agree
with `DESIGN-HASHES.sha256`, and the corrupted value was a current value.

---

## 26. A counter that counts the wrong event freezes work permanently

107 cards on the chi fleet were frozen by `max_claims=5`, and almost none had
failed. The measured cause was seat churn: card `0aec5a64` shows one seat
(`pi-glm-chiap01-0aec5a64`) claiming and releasing its own card 120 times,
median hold 21.4s, median gap 276.8s — the five-minute dispatch tick — with
**nothing written under any hold**. The seat's own wrapper released each time,
and the deterministic ownership hash handed the card straight back.

`_claim_ceiling_hit` is monotonic over an append-only ledger and never
self-clears, so a transient fleet-side defect became a permanent card freeze. A
claim that no live worker worked is not an attempt on the card, and PR #790 stops
charging it: frozen cards go 16 to 9, claims charged across the 181 cards with
five or more claims in 14 days go 2,201 to 1,044, and genuinely worked cards
barely move (`82a101a1`, 124 to 123). The runaway fence holds: `06a95c23` goes
402 to 395 and stays frozen.

The part worth recording is the **rejected** first revision, because it was the
plausible one. It applied the same rule to `churn_breaker.count_claim_attempts`,
reasoning that otherwise the breaker would refuse what the ceiling had just
re-permitted. Measured against the live chi store in a fresh process:

| counting rule | `83e498b6` (the breaker's motivating card) | a pure 120-cycle churn loop |
|---|---|---|
| breaker rule (kept) | 57 attempts | **120** attempts, 1 owner, **refuse** |
| the proposed change | 11 attempts | **0** attempts, 0 owners, **no refusal** |

An unworked hold is not a degenerate stand-in for the breaker's signal, it **is**
the signal: cards reach the churn breaker precisely because no agent *can* work
them, so nothing is ever written under the hold. The change would have disabled
the breaker for exactly the cards it exists to catch, and it broke 8 of the 25
tests in `tests/test_churn_breaker.py`, including
`test_claim_release_cycles_by_one_owner_do_count`, whose name is the contract.
This is contract 10 ("when a test fights a change three times, the test is
winning") landing in the other direction: the tests were right, and the two
tests written to assert the new behaviour were deleted rather than the eight bent.

**Contract.** Two gates that read the same ledger do not therefore share a
counting rule; each names the event it charges and why.

| | |
|---|---|
| Producer | the claim ledger |
| Consumer | the claim ceiling (monotonic, operator-cleared) and the churn breaker (default-off, self-clearing) |
| Recovery owner | the dispatcher seat |
| Evidence | each rule scored against a known-pathological card and against a synthetic pure-churn loop, both recorded |
| Detection | **a card excluded by a counter must log the counter's own inputs** — `CLAIM_CEILING_EXCLUDED` now reports total vs counted claims, the card's `WORKER_DIED` verdict count, and whether an amnesty is in effect |
| Fails closed | an open claim is always charged, an unparseable timestamp is charged, any write under a hold charges it, and each claim is forgiven at most once |

Note that the 120-vs-0 figures are a live-store measurement recorded in PR #790,
not a test. The tests pin the adjacent facts:
`tests/test_claim_ceiling_unworked_claims.py:155` asserts the 120-cycle loop
counts zero *at the ceiling*, and `tests/test_churn_breaker.py:90` asserts a
five-cycle loop counts five *at the breaker*.

---

## 27. A truncated log is a sort order, not a sample

A live chi cycle ended:

```
REVIEW_WITHHELD|chiap08|card=00cd5161|reasons=wrong-seat,absent-typed-metadata,absent-source-binding
... 11 more ...
REVIEW_WITHHELD_OMITTED|chiap08|count=1472
```

The twelve logged cards are the alphabetically-first twelve card ids
(`skfleet-rotate.py:6824`, `_review_withheld[:12]`), so they are not a sample of
anything. The reasons behind the other 1,472 were recorded nowhere, on any host,
while that same cycle closed with `launched=0 attempted=1 receipts=0`.
`absent-typed-metadata`, `wrong-seat`, `absent-source-binding` and
`dependency-blocker` call for four completely different responses, and the
distinction between them is exactly what the log dropped.

The fix is one more line, a full per-reason histogram ordered by weight then
name so two cycles can be diffed (`_review_withheld_reason_histogram`,
`skfleet-rotate.py:6659`).

**Contract.** A truncated list is published with an aggregate over the whole
set, never alone.

| | |
|---|---|
| Producer | any log that caps per-item lines |
| Consumer | the operator reading one cycle |
| Evidence | a complete histogram alongside the capped sample |
| Detection | **a bare `...OMITTED count=N` line with N far larger than the sample is an unanswerable log** |
| Fails closed | the aggregate is emitted even when the sample is empty |

Same shape, different subsystem: `skcapstone.fleet_live_publisher` hardcoded
`"lanes": {}` in every snapshot and ran 41 seconds after the dispatcher, so it
clobbered the dispatcher's truthful lane table each cycle and healthy hosts
advertised capacity 0 (chiap03 advertised 0 in the same cycle its own `SLOTS`
line said `total_free=9`). It now carries the dispatcher's table forward with a
`lanes_ts` provenance stamp, drops it after the readers' 30-minute freshness
fence, and otherwise publishes the explicit marker `"lanes": "unknown"`, which
readers skip instead of summing to zero. **"Could not measure" and "measured
zero" are now distinguishable in every snapshot** — which is contract 9's rule,
enforced in the data rather than in the reader's discipline.

---

## 28. An eligibility verdict that does not check every gate

`coord gates` reports a **claimed** card as eligible. Reproduced in a throwaway
home:

```
coord claim b7ad2d6e --agent probe-writer
coord gates b7ad2d6e
  {"card_id":"b7ad2d6e","eligible":true,"reasons":[],"seat":null,...}
```

`src/skcapstone/coord_gate_diagnostic.py:42-51` computes `dependency_blocked`,
`owned=card.owner is not None`, `capacity_available=busy < target` and
`dependency_blocker_holds`, and passes all four to
`governed_review_gate_reasons`. That function opens
(`src/skcapstone/review_admission.py:288-289`):

```python
    normalized = {str(label).strip().lower() for label in labels}
    if "review" not in normalized:
        return ()
```

For any card without the `review` label it returns empty **before consulting a
single one of those arguments**. The caller then computes
`eligible = not reasons` (`:58`), so on a normal card the only reachable reasons
are `do-not-claim` and `terminal`. Owned, dependency-blocked and over-capacity
all read eligible. The inputs were gathered correctly and discarded.

The workspace binding is the same gap seen from further out. It is checked in
three places — at claim time in the dispatcher
(`skfleet-rotate.py:7354-7367`, via `_source_workspace_spec` at `:642` and
`_verify_source_workspace`), at offer time in `builder_dispatch`
(`src/skcapstone/fleet/builder_dispatch.py:273-305`, builder-eligible cards
only, consumed by a log line), and at authoring time in `source_binding_meta`
(`src/skcapstone/source_binding.py:35-80`, now wired into `coord create` and the
MCP `coord_create`, so a new unbound source-only card can no longer be
authored). It is checked by **no surface that answers "can this card be
worked?" for an operator**: not `coord gates`, not `coord board`/`kanban`, no
MCP tool, and not the dispatcher's own pool construction
(`_claimability_reason`, `_pool_v2_*`). That last one is why a sweep that
repaired 262 bindings did not move the pool's `ready` count at all.

One distinction to keep: `gates` does emit an `absent-source-binding` reason
(`review_admission.py:298`), but that is the governed-review binding
(`link_source_card` plus a 40-hex `link_head_revision`) on review-labelled cards
only. It is a different thing from the dispatcher's
repository/`base_ref`/`base_revision` workspace binding, and reading one as the
other is how the gap stayed hidden.

The adjacent instance is a card whose own fields disagree. `1960a101` folds on
chiap01, chiap03 and chiap08 alike as `status=Column.DOING` with
`archived=True` and `meta.voided=True` (voided 2026-09-16T07:25:31Z, two
minutes after its last `move` to `doing`). The cause is in the fold itself:
`skcoord/card_store.py:1495-1502`, the `void` branch sets `archived`, clears
`owner` and `_claim_revision`, and writes the `voided*` meta — and **never
touches `card.status`**. `void_terminal_actions` suppresses only *subsequent*
moves, so any card voided out of `doing` folds as DOING permanently. One field
is a lifecycle projection, the other a flag, and nothing asserts they agree.

**Contract.** A surface that answers "can this be worked?" runs every predicate
the dispatcher runs, or it answers a narrower question and says so in its name.

| | |
|---|---|
| Producer | the eligibility diagnostic |
| Consumer | the operator deciding whether to intervene |
| Recovery owner | the seat that owns dispatch |
| Evidence | every argument a gate accepts is either used on every path or not accepted |
| Detection | **an early `return ()` placed above the use of the caller's arguments is the bug; a test that passes each blocking input in isolation and asserts a non-empty result catches all four at once** |
| Fails closed | an unchecked predicate makes the answer `unknown`, not `eligible` |

The cheap version of the whole contract: if a function takes four arguments and
has a path that reads none of them, the caller's four measurements were wasted
and the caller cannot tell.

---

## 29. Success printed over an empty write

`coord link` prints a green success line and stores an empty value. Measured in
a throwaway home:

```
rc 0   "Linked d05bcdd1: verdict = ."
STORED {"card_id":"d05bcdd1","action":"link",...,"link_key":"verdict","link_value":"",...}
```

`src/skcapstone/cli/coord.py:1632-1643` appends the `CardEvent` and prints at
`:1634`. The only value guards are `validate_blocked_verdict` and (since #788)
`validate_provisional_verdict`; there is no empty check, and `CardEventLog.append`
validates only card foldability, void state and path safety.

This is the write half of contract 23's read half: the reader discards silently,
the writer accepts silently, and the operator sees green at both ends.

**Contract.** A write path validates its value before it prints success, and
the success line reports what was **read back**, not what was passed in.

| | |
|---|---|
| Evidence | a readback through a different process after the write |
| Detection | **audit the store for empty values on keys that are never legitimately empty; the count should be zero** |
| Fails closed | an empty value on a semantic key is refused at the CLI, as `BLOCKED` and provisional `PASS` already are |

---

# Shape 3: the change that silently reverts

Contract 6 of the 2026-09-18 document said merged is not running. These are the
four mechanisms by which a change that *was* running stops, with nothing
reporting the transition.

---

## 30. Disabled by rename is not disabled

On 2026-09-10 the dead glm lane was turned off with a systemd drop-in, and then
the drop-in was renamed:

```
~/.config/systemd/user/skfleet-rotate.service.d/
    99-disable-unhealthy-glm.conf.resolved-20260910T2143Z   # 193 B, Environment=SKFLEET_GLM_TARGET=0
    50-glm-restore.conf                                     # Environment=SKFLEET_GLM_TARGET=2
```

systemd reads only `*.conf`. The rename did not archive the change, it
**reverted** it, handing the lane back to the older `50-glm-restore.conf`. This
happened on exactly three hosts — chiap01, chiap02, chiap03 — and the reason
only three is the same mechanism again: those three also have
`90-codex-only.conf.rollback-d9a1000f` renamed away, while chiap04 and chiap08
keep an active `90-codex-only.conf`.

It is verifiable in behaviour, not just in the file listing: the journal's
`SLOTS|` line flips from `glm=0/0` to `glm=0/2` on those three hosts after
Sep 10 and stays `glm=0/0` on chiap04 throughout. It was closed on 2026-09-18
23:56 by `99-glm-dead-lane.conf` — a plain `.conf` this time — after which all
three log `glm=0/0` again.

**Three claims about the consequences did not survive measurement, and the
third is the most instructive finding in this document.**

Counting every `pi-glm-*` claim and release across the whole store, per day:

| day | chiap01 | chiap02 | chiap03 | median worker life |
|---|---|---|---|---|
| 2026-09-03 | 139 | 48 | 90 | 21.1-21.4s |
| 2026-09-04 | 49 | 32 | 53 | 21.8-25.6s |
| **2026-09-09** | **229** | **231** | **238** | **20.5-20.7s** |
| 2026-09-10 | 17 | 21 | 15 | 378s / 247s / 35s |
| 2026-09-11 | 3 | 2 | 1 | ~20-45 min |
| 2026-09-12 to 09-17 | 0 | 0 | 0 | — |
| 2026-09-18 | 9 | 2 | 4 | minutes to hours |

- The 20-second worker death with a 5-minute redispatch is **real** (median
  `pi-glm-*` lifetime 21.9s on chiap01 over 566 claims; inter-claim gap median
  288-303s). It is not from the post-rename window. It peaks on **2026-09-09**,
  the day *before* the rename, with 1,167 claims across five hosts.
- The nine-day window it was attributed to contains about **34 glm claims in
  total**, six of those days at literally **zero**, and no day with a 20-second
  median. The lane was re-enabled; it was not thrashing.
- **The 503 cause is not evidenced at all.** `bucket_no_eligible_member`
  appears zero times in the journals of chiap01 and chiap03. The 698
  `worker_died` link rows carry only `owner=` and `claim_revision=`, no reason.
  Worker logs are 0 bytes. The only assertion anywhere that these workers died
  on a 503 is **the prose written into `99-glm-dead-lane.conf` itself** — the
  comment block in the file that fixed it.

That last one deserves its own sentence. A fix wrote its own rationale into a
config file, the config file was later read as the record of what happened, and
the rationale became a fact with no measurement behind it. It is the same shape
as everything else here: an assertion that nothing independently verified,
except this time the estate produced the assertion, stored it, and believed it
back.

**Contract.** A disable is observable in the effective configuration, and a
change's rationale is evidence only where it cites a measurement.

| | |
|---|---|
| Producer | whoever disables the lane |
| Consumer | the unit's effective environment, and the next person reading the history |
| Recovery owner | the host's owner |
| Evidence | `systemctl --user show <unit>` for the variable, not `ls` for the file |
| Detection | **audit drop-in directories for files that are not `*.conf`; a renamed drop-in is a silent revert, and the rename is the only record that it ever applied** — and diff the resulting env against the intent |
| Fails closed | a disable that cannot be read back from the effective unit is not applied |

Two smaller rules fall out. Archive by moving the file out of the drop-in
directory, never by renaming it in place. And when writing a postmortem into
the artifact that fixes something, mark which sentences are measured and which
are inferred, because in six months nobody can tell them apart.

---

## 31. An absent key is a working default until the day it is not

The Forgejo runner template renders

```
image: code.forgejo.org/forgejo/runner:{{ skgit.RUNNER_VERSION | default('6.2.2') }}
```

(`v1/ansible/optional/skgit/src/config/skgit/skgit-runners.yml.j2:61`, identical
in all four SKStacks trees), and **no vault anywhere defines `RUNNER_VERSION`**.
Decrypting every vaulted `group_vars` file under `optional`, `shared`,
`standalone` and `core` returns zero hits; the vault the playbook actually
selects carries one runner key, `RUNNER_REGISTRATION_TOKEN`. So every render
produces 6.2.2.

Production runs **11.1.2**. `docker exec skgit-prod-runner forgejo-runner
--version` says so, and the rendered
`/var/data/config/skgit-prod/skgit-runners.yml` differs from its template in
exactly sixteen places: fifteen are `{{ env }}` substitutions and one is line
61, where `11.1.2` was hand-patched into the **output**. Both images are cached
on the host and the playbook's `template:` task carries no `force: no`, so the
next `deploy_skgit-prod.yml` silently downgrades the runner by five major
versions.

Note which way round this is, because the instinct is to get it backwards. The
README (`v1/README.md:450`) is **correct about production**. The template
default is the stale value, the live state is right, and the deploy is what
would break it. A drift check that trusts the repository as the source of truth
would "fix" a working host.

Two claims that did **not** survive measurement, recorded because retelling them
would have made the fix wrong:

- 6.2.2 does top out at node20 (`strings /bin/forgejo-runner` on the cached
  image shows the contiguous literal `node12node16node20`; 11.1.2 adds
  `node24`). But **no workflow on this Forgejo pins node24.** A `git grep`
  across every ref of all 53 bare repos, for `node24` and for
  `actions/{checkout,setup-node,cache,upload-artifact,download-artifact}@v[56]`,
  returns zero hits; the actual pins are `checkout@v3/@v4`, `setup-node@v4`,
  `setup-python@v5`. So "CI could never work" is false. The downgrade is a real
  regression risk for a different and currently unexercised reason.
- This is the **nor** estate (`skstack01-douno`, runners on norap1001, Forgejo
  on norap1002), not chi.

**Contract.** A template default is a value somebody chose once. Where the live
value differs, the deploy is a regression until proven otherwise.

| | |
|---|---|
| Producer | the template default, and the vault that may override it |
| Consumer | the rendered file, and the container it starts |
| Recovery owner | whoever owns the deploy |
| Evidence | render the template with the real selected vault and diff against the live file, **before** the deploy, not after |
| Detection | **a `default(...)` filter over a key no vault defines is an unpinned production value; enumerate them and require each to be either defined or deliberately defaulted** |
| Fails closed | a render that would change a running value stops and reports, rather than writing |

A hand-patched rendered file is the tell. It means somebody already found the
template wrong, fixed the symptom where it was visible, and left the cause where
it will fire again.

---

## 32. A template narrower than the live config is a deletion waiting to run

`app.ini.j2` for skgit renders **30 keys across 9 sections** against the real
selected vault (63 template lines, 39 `KEY =` lines in source, minus the
`{% if skgit.email_enabled %}` mailer block and a guarded `TOKEN`). The live
`/var/data/skgit-prod/config/app.ini` has **52 keys across 14 sections**, and
has not been touched since March.

The deploy task (`deploy_skgit-prod.yml:154-161`) is a plain `template:` with no
`force: no` and no backup. So a wholesale deploy destroys **22 keys** and adds
none back:

```
WORK_PATH                       repository.ENABLE_PUSH_CREATE_USER
server.START_SSH_SERVER         repository.ENABLE_PUSH_CREATE_ORG
server.LFS_START_SERVER         repository.MAX_CREATION_LIMIT
server.LFS_JWT_SECRET           repository.DEFAULT_PUSH_CREATE_PRIVATE
lfs.LFS_START_SERVER            repository.upload.{ENABLED,FILE_MAX_SIZE,MAX_FILES}
lfs.{MAX_FILE_SIZE,MAX_BATCH_SIZE}
git.MAX_GIT_DIFF_LINES          oauth2.JWT_SECRET
git.timeout.{DEFAULT,MIGRATE,MIRROR,CLONE,PULL,GC}
```

Two of those are generated secrets. Losing `oauth2.JWT_SECRET` invalidates every
issued OAuth2 token; losing `server.LFS_JWT_SECRET` breaks in-flight LFS auth.
Neither is recoverable by re-running the deploy.

`START_SSH_SERVER` and `LFS_START_SERVER` appear **only** in that hand-edited
file: not in `app.ini.j2`, and not in `skgit.env.j2` either (the live
`skgit.env` holds 39 `FORGEJO__*` variables and neither key is among them), so
the env-to-ini injection would not restore them. Git-over-SSH on :222 is
Forgejo's own Go server, which is exactly what `START_SSH_SERVER` gates:

```
$ nc skgit.skstack01.douno.it 222     ->  SSH-2.0-Go
$ ssh -p 222 git@skgit.skstack01.douno.it
Hi there, chefboyrdave2.1! ... Forgejo does not provide shell access.
```

and the LFS store behind `LFS_START_SERVER` holds **17 GB in 34,384 objects**.

There is a second, worse layer. The live `skgit.yml` is a render of
`skstacks-v2-work`'s template, **not** of `skstacks-prod`, the tree a deploy
would use: 179 lines matching v2-work up to Jinja substitution, against
`skstacks-prod`'s 162 lines differing in 103. Deploying from the prod tree would
additionally revert the SSH TCP service port label from 222 to 22 and drop a
hardened `pg_dumpall` backup block. **The live configuration was rendered from a
tree that is not the one anybody would deploy from**, and nothing records which
tree produced which file.

**Contract.** A deploy that overwrites a live config file first proves the
render is a superset of what is there.

| | |
|---|---|
| Producer | the template plus the selected vault |
| Consumer | the running service |
| Recovery owner | whoever owns the deploy |
| Evidence | key-set diff of rendered-vs-live, per section, with generated secrets called out separately |
| Detection | **any key present live and absent from the render is a deletion; report the count before the deploy, and fail on a non-zero count** |
| Fails closed | the deploy refuses rather than truncating, and takes a timestamped backup it can restore from |

The provenance rule is the one that generalises past this service: **a rendered
artifact records the template tree and commit that produced it.** Without that
stamp, "redeploy the current config" is not a defined operation.

---

## 33. A deployed artifact is not the repository

`~/.local/bin/skfleet-rotate.py` and `~/.local/bin/skfleet-worker-wrapper.py`
are per-host copies. A `git pull` does not touch them, `pip install` does not
touch them, and `pip show` cannot see them: the wrapper was never in
`pyproject.toml`'s `script-files`, so it belongs to no package a version check
covers.

The estate has measured this repeatedly (`scripts/fleet/skfleet_merged_vs_running.py`,
module docstring): on 2026-09-18 the lane-model-routing fix was merged while all
five chi hosts kept running the pre-fix `skfleet-rotate.py` (md5 `8c400694`),
and during that window the gateway served 467 requests from a 5-slot local
fallback while codex's 32 slots served 6 and zai's 10 served 1. It was found
only because a separate audit happened to look. Earlier: three different
`skmail` binaries across five hosts, none matching the repo;
`skfleet-rotate.timer` active but **not enabled** on all three rotate hosts for
at least seven weeks, so a reboot on any of them would have stopped fleet
dispatch estate-wide with nothing reporting it.

This session's instance is preserved in the pre-deploy backups, and the git
blob hashes date it exactly:

| host | wrapper sha256 (pre-deploy) | size | mtime | = commit |
|---|---|---|---|---|
| chiap01 | `a55a661e…def4` | 26,810 | 2026-09-10 21:54:23 | `3dff19e9` (09-10) |
| chiap02 | `a55a661e…def4` | 26,810 | 2026-09-10 21:54:28 | `3dff19e9` |
| chiap03 | `a55a661e…def4` | 26,810 | 2026-09-10 21:54:32 | `3dff19e9` |
| chiap04 | `a55a661e…def4` | 26,810 | 2026-09-10 21:54:36 | `3dff19e9` |
| **chiap08** | `19b740ce…1ead` | **29,582** | **2026-09-13 00:44:30** | **`e1ada0e7`** (09-12) |

Four hosts sat on the 2026-09-10 build for 8 days 2 hours, through **seven**
subsequent wrapper commits, and chiap08 ran a genuine third version because it
uses a different backup naming scheme, and therefore a different deploy path.
All five converged on 2026-09-18 23:57, staggered about four seconds apart, and
are now byte-identical to each other and to the repo. A drift check run at any
point in those eight days would have reported SPLIT FLEET; none was run.

Every PR in this session that changed the dispatcher or the worker brief ends
with the same line, and it is not boilerplate: **merging changes nothing until
the artifact is copied.** PRs #777, #788 and #790 all ship behaviour that does
not reach a live worker until a deploy runs.

**Contract.** The unit of deployment is the artifact, and the check compares
content digests on the host against the merged ref.

| | |
|---|---|
| Producer | the merged ref |
| Consumer | the path each unit actually executes |
| Recovery owner | Operations |
| Evidence | per host, per artifact: OK / DRIFT / UNKNOWN by content digest, never a version string |
| Detection | **SPLIT FLEET is a distinct and worse finding than uniformly-behind** — hosts that disagree with each other disagree about semantics |
| Fails closed | an unreachable or unmeasurable host is UNKNOWN, never OK |

The tooling for this already exists and is read-only
(`skcapstone fleet node drift`, `skfleet_merged_vs_running.py`,
`docs/fleet/rollout-drift.md`). The gap is not the check. The gap is that
nothing runs it on a timer, so it answers the question only when somebody
already suspects the answer.

---

# Verification discipline: the diagnosis failed too

Contract 9 of the 2026-09-18 document already said to verify through a different
path than the one that wrote it. This session produced enough new instances to
make the rule sharper, and two of them are the most valuable findings here.

---

## 34. The fold is the only authority

`CardStore.fold()` (`skcoord/src/skcoord/card_store.py:1440`) is not a
convenience wrapper over a list of events. It merges the union of the card's own
store logs **and** the sanctioned legacy paths (archive index plus the
`card_events` overlay) in `(ts, writer, seq)` order. Any analysis that iterates
one store, or that orders events by file position, is a different algorithm that
happens to resemble it.

The cost of forgetting that was paid four times in one session, each time
producing a confident number that was wrong, and each time caught by the author
rather than by a reviewer:

- **13 stuck claims that were not claimed.** A `claims.py` replay treated
  `move`-to-done and `void` as the only terminal actions and missed `complete`.
  Eleven of the twelve cards it named had no owner at all. The fold's answer was
  six claims, all recent.
- **2,866 live SKLEGAL cards on a 1,160-card board.** Same missing terminal
  actions, this time also missing `archive`. Caught by the subset being larger
  than its set, which is contract 9's detection rule doing its job. The real
  figure was 607 of 1,160.
- **15 successful writes reported as total failures.** The verifier read
  `verb`/`kind`/`type` where the schema uses `action`, so it returned zero
  regardless of what the command did. A bug report against a working `coord
  label` was nearly filed.
- **A claim census diverging on 75 of 143 cards.** Deleted — and then the same
  pattern was rebuilt in the fleet monitor, which reported "27 held, 11 stuck"
  where the authoritative answer was 19 and 2. Deleting a bad replay does not
  help if the shape is reintroduced downstream.

This is also why PR #786 existed. Inside the dispatcher itself,
`_load_outcomes` and `_provisional_candidate` read both stores while
`_matching_outcome_events` and `_generation_invalidated` read only the structure
store. `coord link` writes to the overlay, and `coord link` was how nearly every
provisional PASS on this fleet was recorded, so the review opener selected an
outcome and then failed to find the event behind it. Measured on chiap01: of the
214 cards blocked that way, **212 have exactly one matching outcome event and it
is in the overlay every time**. The opener reported cards as missing evidence it
had never looked for.

**Contract.** Board facts come from the fold. A replay is permitted only as a
test *of* the fold, never as a source of a finding.

| | |
|---|---|
| Producer | `CardStore.fold()` |
| Consumer | every count, audit, report and dashboard |
| Recovery owner | the store's owner |
| Evidence | the reader is the shipped fold, called as an API, not reimplemented |
| Detection | **one helper returning the identity-deduped union of both stores, with every reader routed through it** (`_outcome_scan_rows`), so "every event that can carry an outcome" has exactly one definition |
| Fails closed | a reader that cannot reach one store reports UNKNOWN, not zero |

---

## 35. Re-fold in a fresh process

`CardStore` caches legacy mutations per instance and never invalidates them:
`self._legacy_cache` (`card_store.py:452-455`), loaded once in `_legacy_events`
(`:1431-1438`). A long-lived instance therefore serves overlay state frozen at
its first fold, and a link written after that read back as `None`.

The same method has a second, quieter mode: if `load_legacy_mutations` raises,
the cache is set to `{}` and a warning is logged, after which every fold on that
instance silently returns no legacy events at all. One warning, then an
indefinite stream of confidently wrong folds.

**Contract.** A measurement of live state is taken in a process that has not
already read it.

| | |
|---|---|
| Evidence | fresh process, fresh `CardStore`, for every measurement quoted in a report |
| Detection | **re-run the measurement in a new process before it becomes a finding; two runs that disagree mean the cache, not the board** |
| Fails closed | a degraded cache load fails the fold rather than serving an empty legacy set |

Every board measurement cited in this session's PRs says "measured in a fresh
process" for exactly this reason, and that phrase should be read as a claim
about method, not as a flourish.

---

## The one-line version

The 2026-09-18 document's one-line version was: every failure was an unverified
assertion. A day later the sharper statement is about *where* the verification
was missing.

- **Shape 1** is a missing check on **reachability**. Nothing asserted that the
  mechanism ever ran. Detection: count the mechanism's successful outcomes over
  a long window, and treat a hard zero as an alarm equal to a failure.
- **Shape 2** is a missing check on **provenance**. Nothing asked who computed
  the field. Detection: for every signal a decision depends on, name the process
  that produced the value, and check whether it is the process the reader thinks
  it describes.
- **Shape 3** is a missing check on **persistence**. Something verified the
  change once. Detection: compare content digests of what is running against
  what was merged, on a timer, and report split state as worse than behind.

All three are the same omission at different points in time: nobody checked that
the thing kept being true after the moment it was proven.

---

## 36. A refusal is a finding, and it has to be invited in writing

Twice in this session an agent was told to destroy something and correctly
refused.

**Three of four "runaway" cards were finished work.** A brief named 106 frozen
cards as runaways and listed four worst offenders to void. `0aec5a64`,
`22244103` and `2587020f` each carry `verdict=PASS_FOR_REVIEW`, a hash-bound
evidence bundle (`evidence_sha256=59265a3d…`, `a5d9cc0a…`,
`artifact_sha256=57354d7a…`), commits, and open PRs on sklegal (#146, #147).
`0aec5a64` even had a review card, `8c6ed8e4`. Only `06a95c23` — 402 claims,
8 releases, zero artifacts — was a genuine runaway, and it was voided.

(Those PRs are **open**, not merged. The story was retold once with "merged
PRs" in it, which is a small thing and exactly the kind of small thing this
document exists to stop. `gh pr view` says OPEN.)

**A sha that could not be resolved was a typo, not a fabrication.** A review
card, `3b172df0`, carried a review-target sha that resolved to nothing, and the
instruction was to void it. Its producer card `19109200` carries the real
commit:

```
on the review card (38 hex)   3b172df021eb2776e5e8d0e042ed7aa8aa9758
on the producer card (40 hex) 3b172df021eb2776f5e8e9d0e042ed7aa8aa9758
```

Sixteen characters of shared prefix, one substitution at index 16, two dropped
characters. Provenance: `pi-codex-chiap01-19109200` wrote the correct 40-char
value as a link at 17:02:03 and created the review card thirty seconds later
with the mangled 38-char copy. Of eleven cards in that batch, ten were voided
and this one was refused.

**Contract.** Any brief that authorises destruction carries an explicit
verify-first clause, and a refusal is reported as a result rather than an
exception.

| | |
|---|---|
| Producer | whoever writes the destructive brief |
| Consumer | the agent executing it |
| Recovery owner | the brief's author |
| Evidence | per item: the stated reason, re-verified against a fresh fold, before the action |
| Detection | **a destructive batch that reports 100% completion is a batch nobody checked** — a refusal rate of exactly zero over a large batch is suspicious, not reassuring |
| Fails closed | an item whose stated reason does not hold is reported, never actioned |

The instruction has to be in the brief. The batch that caught the sha typo was
told, in the brief, *"Do not void on my say-so. For each card, verify
independently… If any card's stated reason does not hold up, do not void it.
Report it instead."* The batch that caught the three finished cards had only a
narrow "do not void a card that is already terminal" and its agent went well
beyond what it was told. One of those two is a repeatable mechanism and the
other is luck, and they should not be recorded as the same thing.

---

## 37. The first number is not the number, and some numbers are not numbers

A verification run reported **1 local test failure**. Rechecked, the full suite
was **3 failed, 9,707 passed, 14 skipped**. The first figure came from a `-x`
run that stopped at the first failure; the second came from letting it finish.
All three were then verified against `origin/main` and were pre-existing.

One of the three does not have a value at all.
`tests/test_skfleet_dispatch_integrity.py::test_five_host_candidate_inventory_counts_unique_ids`
reads ambient live fleet state: `_run_watch_sample` (`:143-192`) passes the
whole environment through (`env={**os.environ, "HOME": str(tmp_path), …}`) into
the real `scripts/fleet/skfleet-distribution-watch.sh`, whose host list, local
host and state directory all come from the environment with fleet defaults, and
whose `sample()` ssh-probes every host that is not `$local_host`. The test's
`ssh` stub matches `chiap01|chiap02|chiap03|chiap04`; **chiap08 falls straight
through it to the real network.** Four back-to-back runs of that file on
`origin/main`, same machine, no edits:

```
run 1: 12 passed
run 2:  1 failed   (state=collector_fault)
run 3:  2 failed
run 4:  1 failed   (state=zero)
```

A second test in the same file, `test_escalation_only_sessions_keep_distribution_watch_up`,
fails on the same mechanism.

**Contract.** A test result is a number only if the same commit produces it
twice.

| | |
|---|---|
| Producer | the test suite |
| Consumer | anyone deciding whether a change is safe |
| Recovery owner | the test's owner |
| Evidence | a full run, not a `-x` run, and each failure attributed against the base ref before the change is blamed or cleared |
| Detection | **run a suspect test file three times on the same commit; a differing result means it reads ambient state, and it should be quarantined rather than re-run until green** |
| Fails closed | a non-deterministic test is a failing test — a suite that is green only sometimes is not a gate |

The `-x` detail generalises. **An early-exit run reports the position of the
first failure, not the count of failures**, and the two get written down the
same way.
