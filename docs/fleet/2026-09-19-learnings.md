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
