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
(`scripts/fleet/skfleet-rotate.py:5462`) was 0 across 14 days and 1,660
rotations on chi, while 214 cards logged `OPEN_REVIEW_EVIDENCE_BLOCKED`
(`skfleet-rotate.py:5304`) every single cycle.

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
recorded every fact the opener needs, as six separate `coord link` rows at six
timestamps:

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
assembled. Measured across the 354 cards ever reported blocked this way, 305
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
(`tests/test_skrsi_handoffs.py:398,402`). The board agrees: across the local
card-event store, 1,076 `link` events carry **zero** occurrences of either key.

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

Measurement on the two live chi long-runners made the same point from the other
side (PR #777): beat ages of 10s and 26s on workers 4.9 and 4.2 hours in,
against a worker stdout log that was 0 bytes for the entire run and last card
events 2.2 and 4.0 hours stale. The beat separates *alive* from *dead*. It says
nothing at all about *working*, which is the question every consumer was asking
it.

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

`coord gates` reports a card eligible without ever checking its workspace
binding. `src/skcapstone/coord_gate_diagnostic.py` (62 lines) checks
unknown-card, dependency-blocked, the governed review gate reasons, a
`do-not-claim` label, and terminal states, then sets `eligible = not reasons`
(`:58`). The words workspace, worktree, repository, `base_ref` and
`base_revision` do not appear in the file.

The dispatcher gates on exactly that, later and separately
(`skfleet-rotate.py:7354-7367`, `WORKSPACE_BLOCKED`, via `_source_workspace_spec`
at `:642`, which raises on conflicting, partial or invalid bindings). So a card
can read `"eligible": true` from the diagnostic and never dispatch, and the
operator surface and the dispatcher disagree with no way to see it.

The adjacent instance is a card whose own fields disagree: a folded
`status=DOING` sitting on `meta.voided=true, archived=true`. One is a lifecycle
projection, the other is a flag, and nothing asserts they agree.

**Contract.** A surface that answers "can this be worked?" runs every predicate
the dispatcher runs, or it answers a narrower question and says so in its name.

| | |
|---|---|
| Producer | the eligibility diagnostic |
| Consumer | the operator deciding whether to intervene |
| Recovery owner | the seat that owns dispatch |
| Evidence | the diagnostic and the dispatcher share one predicate list, enumerated from the code |
| Detection | **fold the board and diff the two answers; any card where they disagree is a defect in the diagnostic** — and a cross-field consistency pass (status vs `meta.voided`/`archived`) is the cheap version |
| Fails closed | an unchecked predicate makes the answer `unknown`, not `eligible` |

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

The cost of forgetting that was paid three times in one session, each time
producing a confident number that was wrong.

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
