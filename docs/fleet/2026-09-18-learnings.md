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

The cheap version of this rule: **a surprising number is a claim about your
query before it is a claim about the world.**

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

## The one-line version

Every failure here was an unverified assertion, and the fix is always the same
shape: name the boundary, name the owner, make the evidence machine-checkable,
and make the check fail closed. SKRSI already says this. The work is applying
it to the boundaries that did not have it yet.
