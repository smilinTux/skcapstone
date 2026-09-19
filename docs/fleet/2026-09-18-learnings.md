# What broke on 2026-09-18, and the contracts that would have caught it

One day on the chi estate surfaced eleven distinct failures. They look
unrelated (a routing bug, a charter violation, a CI misconfiguration, a
deployment gap) and they are not. Every one has the same shape, which is the
shape SKRSI already names:

> **Something asserted a state, and nothing independently verified the
> assertion against reality.**

SKRSI's answer to that is a boundary contract: a named producer, a named
consumer, a named **recovery owner**, machine evidence that **fails closed**,
and a runtime that **cannot qualify itself**. This document applies that frame
to each failure, because a learning written as advice gets read once and a
learning written as a contract gets enforced.

Each section states the failure, why it was silent, and the contract. The
contract is the deliverable. "Be more careful" is not a contract.

---

## 1. A function can be correct and unreachable at the same time

`_lane_model` resolved a card to the model its lane actually uses. It was
correct. It was called by **nothing**, so every lane shipped the bare size
bucket instead, and the estate's paid capacity sat idle for days.

**Why it was silent:** a unit test of `_lane_model` passed the entire time. The
function was never wrong. Only the call site was missing, and nothing tested
the call site.

**Contract.** For any function whose whole purpose is to be called on a hot
path, the test asserts the **call site**, not just the function.

| | |
|---|---|
| Producer | the resolver function |
| Consumer | the dispatch path that must call it |
| Recovery owner | the lane's seat (niobe) |
| Evidence | a source-level or integration assertion naming the call site |
| Fails closed | the guard fails when the call site disappears, not when the function breaks |

A test of the function alone is evidence the function works. It is **not**
evidence the system uses it. Those are different claims and only one of them
was ever checked.

---

## 2. The dangerous failure is the one that returns success

The bare bucket `sk-m` is a **valid** gateway route. It resolves to the local
qwen fallback. So a card dispatched to the codex lane asked for `sk-m`, was
answered by qwen, and came back with perfectly good work.

An unadvertised model would have thrown loud 404s and been fixed in an hour.
A valid route to the wrong backend produced no error at all, and ran for days.

**Contract.** Where a request can be satisfied by more than one backend,
**success is not evidence of correct routing**. The evidence is *who served
it*.

| | |
|---|---|
| Evidence | served-by identity per request, compared against the intended lane |
| Detection | a backend with a large `max` and near-zero traffic is an alarm, not a curiosity |
| Fails closed | a lane whose requests are all answered by a fallback reports as misrouted |

The general rule: **when diagnosing, ask what a healthy system would look like
that this one does not.** Zero requests on a 32-slot backend was visible all
along. Nobody was looking, because nothing was failing.

---

## 3. A fail-closed gate must be able to observe its own recovery

Lane admission refused kimi as `unknown` forever. Not because kimi was
unhealthy: the snapshot said `healthy`. The snapshot listed the
`(kimi, kimi-for-coding)` binding **twice**, because the kimi alias loop
appended unconditionally while the glm and codex loops were guarded.
`lane_health()` requires exactly one exact match, so two *identical healthy*
rows read as ambiguity.

No amount of gateway recovery could clear that. The gate's input was the
writer stating a row twice.

**Contract.** Every fail-closed gate declares the **event that reopens it**,
and a test proves that event actually reopens it.

| | |
|---|---|
| Producer | the snapshot writer |
| Consumer | the admission gate |
| Recovery owner | the lane's seat |
| Evidence | a test that drives the gate closed and then reopens it by the declared event |
| Fails closed | ambiguity still refuses; identical duplicates collapse to one observation |

A gate that can refuse but cannot be shown to un-refuse is not fail-closed, it
is **stuck**, and the two are indistinguishable from the outside.

---

## 4. A distributed fence that reads local state is not a fence

Two hosts ran the same card simultaneously. Every fence on the claim-then-launch
path (the fresh refold, the post-claim identity read, the under-lock recheck)
reads the **host-local** store, and the admission lock is a local lock file. A
claim still in Syncthing flight is invisible to all of them, so both hosts pass
every check and both launch.

**Contract.** A mutual-exclusion check names the scope it actually covers, and
anything crossing that scope gets a **second fence at the point of use**.

| | |
|---|---|
| Producer | the claiming host |
| Consumer | the worker about to do the work |
| Recovery owner | the dispatcher seat |
| Evidence | at startup, the worker folds the card and confirms it is the owner |
| Fails closed | a non-owner exits **without releasing, voiding or writing** anything |

The loser must touch nothing. It does not own the card, so it has no business
writing to it, and releasing another owner's claim is how live work gets
stolen. A residual window remains (an interloper starting before the winner's
claim arrives) and is stated honestly rather than papered over.

---

## 5. An identity is a claim about who acted, and it must be unforgeable by accident

Two instances, same root:

- The **read-only** Overseer wrote 324 moves and 147 claim-releases. Its own
  timer never mutated anything: 90% of those landed in one burst from a
  controller session using `--agent mero`. A human-driven tool wore the seat.
- The **dispatcher** writes as `jarvis`, 4,487 events after the seat contract
  moved dispatch to niobe. The job is done correctly under the wrong name.

**Contract.** A seat identity is assumed by the seat's own runtime, never by a
tool's flag.

| | |
|---|---|
| Evidence | writer identity derives from the running seat, and a controller cannot assert one |
| Detection | an audit query over the event store flags any writer acting outside its verb allowlist |
| Owner of the check | **a different seat than the one being checked** |
| Fails closed | an unknown identity is refused, not allowed |

Two details matter more than the rule. First, the check is owned by another
seat, because a measurer that checks itself is circular. Second, the
authorization was a **deny-list with one entry**: Jarvis was checked, everyone
else passed. Adding a second name to a deny-list does not fix a deny-list, and
the correct shape is a capability table that **refuses an identity it does not
know**.

---

## 6. Merged is not running, and version strings lie

Three merged PRs were running on **zero** hosts. One dispatch host ran a
feature branch with uncommitted edits, code present in no merged ref. Its pip
metadata still reported a plausible version.

This is the estate's oldest repeat: previously a release was merged, published
and PyPI-verified at 07:00 and not installed until 23:45, while `pip` said
`0.1.56` and the module said `0.1.0` **on the same host**.

**Contract.** Deployment verification compares **content digests**, never
reported versions, and runs on a timer rather than on someone remembering.

| | |
|---|---|
| Producer | the merged ref |
| Consumer | every host's resolved import path |
| Recovery owner | Operations |
| Evidence | sha256 of the artifact the unit's **own interpreter** resolves, per file |
| Fails closed | unreachable host is UNKNOWN, never OK |

Two refinements earned the hard way. Resolve each unit's interpreter from its
**effective** `ExecStart` (a drop-in may override the unit file, and parsing
the file names the wrong interpreter). And report **split fleet** as a distinct,
worse finding than uniformly-behind: hosts that disagree with each other
disagree about semantics.

---

## 7. Do not point a long-running service at a tree that agents work in

On every chi host the dispatcher imports from the same tree fleet workers do
card work in. Consequences observed within ten minutes on one host: the tree
sat on a feature branch with five uncommitted files on no remote, and the same
library file was edited three separate times while five workers ran.

A worker editing that path changes the library **under the running dispatcher
with no deploy step**, and it makes the host undeployable without destroying
in-flight work.

**Contract.** The path a service resolves its code from is release-managed and
unwritable by workers. Workers get their own worktree.

| | |
|---|---|
| Evidence | the service's resolved import path is not inside any tree with a branch checked out for work |
| Fails closed | a deploy refuses a dirty tree rather than resetting over it |
| Recovery owner | Operations |

The refusal is the point. When the deploy hit a dirty tree it **stopped**, and
five pieces of unsaved work were committed and pushed instead of erased. A
split fleet for a few hours is recoverable; a destroyed worker's work is not.

---

## 8. CI that runs twice starves the job that matters

`secret-scan` and `docs-check` used unfiltered `push:` triggers, so every PR
ran them twice: same commit, same result, double the runners. The `unit` job
had **no `timeout-minutes`**, so a hang would hold a required check for six
hours.

And the job that appeared broken was not. `cancel-in-progress` means every
force-push kills the in-flight run. The short lane finished inside the gap
between pushes; the full suite did not, so only the full suite ever showed as
cancelled. **A merge queue rebasing on a short interval cancels the very check
it is waiting for.**

**Contract.** Triggers are branch-scoped, every long job is bounded, and an
automated queue must not push while the check it waits on is in flight.

| | |
|---|---|
| Evidence | one run per workflow per commit; a bounded timeout on every job |
| Detection | a job whose history is mostly `cancelled` is a queue problem, not a job problem |
| Fails closed | a hang fails in minutes, not hours |

Before deleting a slow gate, check whether it is slow or being killed. The full
suite here is ~10 minutes for 9,303 tests, which is the honest cost of the only
complete test gate in the repo.

---

## 9. Verify through a different path than the one that wrote it

This applies to the work of diagnosis itself, and it cost real time today.

- A verifier read `verb`/`kind`/`type` when the schema uses `action`. It
  reported **15 successful writes as total failures**, and nearly produced a
  bug report against a working command.
- A fold counted terminal cards using only `move`-to-done and `void`, missing
  `complete` and `archive`. It reported more open cards in one family than
  existed on the whole board.
- An audit ran against the local `~/.skcapstone`, which is the **nor** estate,
  while the question was about **chi**. Disjoint stores, unusable result.
- A local checkout of the standards repo was **stale**, producing a confident
  and wrong claim about what the roster contained.

**Contract.** A negative result is not a finding until the reader is validated.

| | |
|---|---|
| Evidence | confirm the field names and the scope before trusting a zero |
| Detection | a count that contradicts a known total (a subset larger than its set) is a bug in the counter |
| Fails closed | "could not measure" is reported as distinct from "measured zero" |

A fourth instance arrived after that rule was written, which is the point of
recording it. A backlog was reported as "about 90 older PRs" from
`gh pr list --limit 100` returning exactly 100. The real figure was **293**.
The limit was the answer.

**A round number equal to your own limit is not a measurement.** Any count that
lands exactly on a page size, a `head`, a `LIMIT`, or a default is a reading of
the query, and the query has to be re-run unbounded before the number means
anything.

The cheap version of this rule: **a surprising number is a claim about your
query before it is a claim about the world.** The cheaper version: **a number
equal to your limit is always your limit.**

---

## 10. When a test fights a change three times, the test is winning

A change broke a source-level guard. The guard was updated, correctly, because
it pinned the exact line being rewritten. Then a second test failed. Its
fixture was updated. Then a third assertion failed, and a fourth.

At that point the change was narrowed instead, and it was the right call: the
change had been applied to governed review dispatch, which had **already
selected a concrete route** using the reviewer seat, producer identity and
per-route occupancy. Overriding that made route preflight refuse every
candidate and launch nothing, on the estate's single most active lane.

**Contract.** Bending a second assertion in the same file is a signal. Bending
a third is a stop.

| | |
|---|---|
| Evidence | each assertion changed is justified against the invariant it encodes, not against making the build green |
| Fails closed | when the justification is "so my change passes", the change is wrong |

The distinction worth keeping: updating a guard that pins a line you
deliberately rewrote is legitimate. Updating an assertion because the behaviour
it protects is inconvenient is not. The first names a new invariant; the second
deletes an old one.

---

## 11. An unowned job is how all of this happened

The gap analysis found jobs that were **owned but performed by nobody**:
release and install, deploy verification, merge execution, and verification
that recorded human decisions took effect. Two idle seats split one undone job
and made each other unaccountable.

**Contract.** Every job in the estate has exactly one recovery owner, and the
roster names it. Dissolving a seat requires naming where each of its duties
goes.

| | |
|---|---|
| Evidence | a gap-analysis row per job: owner on paper, performer in the measurements, verdict |
| Detection | `OWNED-BUT-NOBODY-DOES-IT` is a finding, not a footnote |
| Fails closed | a seat cannot be removed until its duties have a named destination |

This is SKRSI's boundary table applied to people. Every SKRSI contract names a
recovery owner precisely so that no boundary's failure is somebody's problem in
general and nobody's in particular.

---

## 12. Coverage is the property. A list of gated entrypoints is not coverage

The Overseer mutating the board looked like one seat overstepping. Enumerating
the code found the real shape: of **32 coord mutation entrypoints, 22 had no
authorization call at all**. `void`, `describe`, `label`, `link`,
`reprioritize`, `amend-criteria`, both dependency verbs and `satisfy-gate` were
ungated. The ten that were gated checked exactly one identity by name and let
every other actor through.

So the story was never "a deny-list missing an entry". It was a handful of
guarded doors in a building with no walls.

**Contract.** The property is that **no mutating entrypoint lacks a gate**, and
a test enumerates the entrypoints from the code to prove it.

| | |
|---|---|
| Evidence | an AST walk over every CLI command and MCP handler, asserting the gate call |
| Fails closed | **a new verb counts as mutating until deliberately classified read-only** |
| Recovery owner | the seat that owns the surface |

That default is the whole mechanism. A hand-maintained list of what to check
degrades every time someone adds a verb, and nothing notices, which is exactly
how `release-claim` ended up with no check while its four siblings had one.

This generalises past authorization. Wherever a rule must hold for a *class* of
things, test the class membership, not the members you remembered.

---

## 13. An identity must be a subject, not whatever string arrived

The writer `SKAGENT` wrote **12 events in 14 days**. Not the value of the
`SKAGENT` variable: the literal, unexpanded variable name, from a shell that
did not interpolate it. The board accepted it as an actor and attributed real
mutations to it.

Alongside it, the same audit found a controller wearing a seat's identity via
`--agent mero`, and the dispatcher's own writer arriving from a second
unnoticed spelling, `os.environ.get("SKAGENT", "skfleet-rotate")`, where the
chi hosts export `SKAGENT="jarvis"` in `.bashrc`. The same code path therefore
produced two different identities depending on whether it ran from a login
shell or a timer.

**Contract.** An actor is resolved and validated, never accepted as a free
string.

| | |
|---|---|
| Evidence | the identity matches a known subject or a declared delegate grammar |
| Fails closed | an unknown string is **refused**, so a hostname or an unexpanded env var cannot act |
| Detection | audit for writers matching no known subject; the count should be zero |

The fix for the dispatcher deliberately does **not** consult `SKAGENT`, with a
test pinning that inertness, because honouring it would silently restore the
wrong writer on every interactive run on those hosts. When an input has proven
it can carry a wrong value, refusing to read it is a legitimate fix.

---

## 14. A seat retired on paper keeps writing until something checks

Tank was dissolved into Operations on 2026-09-17. It wrote **18 events on
2026-09-16**, and the trailing writer was still live when the authorization
table was built, so its capability row had to be kept rather than deleted, with
a follow-up to find what still wears the identity.

**Contract.** Retiring a seat is not a document edit. It completes when no
writer bearing that identity has acted for a defined window.

| | |
|---|---|
| Evidence | zero events under the retired identity since the retirement timestamp |
| Fails closed | the capability row stays until the writer is found and stopped |
| Recovery owner | whoever absorbed the duties |

Deleting the row first would have broken a live writer. Keeping it is honest:
the row now documents a known gap instead of asserting a tidiness that is not
true yet.

---

## 15. Name the boundary you cannot reach yet

The authorization standard says one PDP: `capauth.authz.decide` over canonical
fqids. The coord CLI cannot reach it. `--agent` is an unauthenticated string
with no credential to resolve a subject from, so the policy lives in one local
decision function until coord subjects are enrolled.

That limit is written into the PR rather than papered over, and the thin-PEP
side is built so it survives the migration unchanged.

**Contract.** When a standard cannot be met yet, the gap is stated with the
condition that closes it, and the code is shaped so closing it is a
substitution rather than a rewrite.

| | |
|---|---|
| Evidence | the unmet requirement named, with the precondition that unblocks it |
| Fails closed | the interim mechanism still refuses unknown subjects |

An honest "not yet, and here is why, and here is what it takes" is worth more
than a green checkbox over a local reimplementation nobody knows is local. The
estate's own standards say green-by-omission is worse than no standard.

---

## 16. Two queues into one trunk will starve the expensive one

Two merge queues ran against the same repository: one for this session's code
fixes, one for a backlog of small docs PRs. The docs queue merged five PRs in
eight minutes, because docs clear CI in about a minute. The code queue merged
nothing at all.

The mechanism is simple once seen. Every merge moves `main`. Every move knocks
every other open PR to `BEHIND`. A `BEHIND` PR owes a fresh CI run, and the
full suite is about **15 minutes** while the docs lane is about **one**. So the
cheap queue lapped the expensive one continuously, and the expensive one could
never finish a cycle before its base moved again.

Nothing failed. Both queues behaved exactly as written. The expensive queue was
simply never going to converge, and the PRs it held were the ones carrying
production fixes.

**Contract.** Queues merging into one trunk share a resource, and the sharing
must be explicit.

| | |
|---|---|
| Producer | each merge queue |
| Consumer | the trunk every queue rebases onto |
| Recovery owner | whoever runs the queues |
| Evidence | merge throughput per queue; a queue with zero merges while another is landing steadily is starving, not waiting |
| Fails closed | one queue runs at a time, **expensive first**, and the cheap one resumes after |

The instinct to run them concurrently is the wrong one: concurrency is what
creates the starvation. The correct move is to serialise, and to put the
expensive lane first, because the cheap lane loses almost nothing by waiting
while the expensive lane loses everything.

Watch for this shape anywhere a slow consumer and a fast consumer share a
sequencing point. The slow one does not merely go slower. It can be held at
zero indefinitely while every component reports healthy.

---

## 18. A worker can be fully productive and completely invisible

Two workers had run for four and a half hours at about three percent CPU. Every
board-side signal said they were dead: their last CARD EVENTS were 2.2 and 4.0
hours old.

They were working the entire time. Measured directly on their workspaces:

```
9e15f83c   newest write 169s ago    6 files in 10min    216 files in 60min
abe011e9   newest write 628s ago    0 files in 10min  2,351 files in 60min
```

One had written over two thousand files in the previous hour while emitting
nothing the board could see.

The reason low CPU misleads is that an agent waiting on a model is idle by every
process metric. The reason card events mislead is that a worker emits them at
task boundaries, not while working, so a long task looks identical to a dead
one.

**Contract.** Worker liveness is measured by OUTPUT, not by process state and
not by board events alone.

| | |
|---|---|
| Producer | the worker |
| Consumer | any reaper, TTL, or health check that can end a worker's claim |
| Recovery owner | the dispatcher seat |
| Evidence | recent writes in the worker's own workspace, plus dispatcher liveness links |
| Fails closed | absent workspace evidence AND absent events, never either alone |

This is why the claim TTL counts the dispatcher's `worker_liveness` links as
owner activity rather than card events alone: without that, a productive
long-running worker is reaped for the crime of not having finished yet.

The diagnostic that actually separates the cases, used twice today and correct
both times:

- **wedged**: no workspace writes for hours, or no workspace at all
- **working**: writes within minutes, whatever the CPU says

Three workers failed that test and were stopped, freeing their claims cleanly.
Two passed it and were left alone. CPU was ~3% for all five and would have
condemned every one of them.

---

## 19. Capacity is not throughput, and an idle fleet is usually a starved one

The fleet reported 48 worker slots and ran two workers. The obvious reading is
that dispatch is broken. It was not:

```
ready=27            <- actually dispatchable
awaiting_review=357
claim_ceiling=112
safety_filtered=209
not_claimable=153
blocked_backoff=108
dep_blocked=75
```

48 slots chasing 27 cards. And ownership is `hash(card_id) % len(ROTATION_HOSTS)`,
so with a small ready pool most hosts own none of it and correctly log
`SELECTION_EMPTY reason=foreign-hash-partition` while sitting at zero.

Every component was healthy. Adding hosts would have made it worse, by
splitting 27 cards across a larger modulus.

**Contract.** Before treating idle capacity as a dispatch fault, measure the
ready pool and the partition.

| | |
|---|---|
| Evidence | the pool's full exclusion breakdown, every card in a named bucket |
| Detection | `SELECTION_EMPTY reason=foreign-hash-partition` with free slots means STARVED, not blocked |
| Fails closed | a host with free slots and no owned cards reports starvation distinctly from refusal |

The corollary is where the real work is. When ready work is scarce, the
constraint has moved upstream: here, 357 cards sat behind a review gate
reporting `capacity=2 eligible=0` every cycle. Scaling the thing that is idle
never helps when the thing feeding it is stopped.

---

## The one-line version

Every failure here was an unverified assertion, and the fix is always the same
shape: name the boundary, name the owner, make the evidence machine-checkable,
and make the check fail closed. SKRSI already says this. The work is applying
it to the boundaries that did not have it yet.
