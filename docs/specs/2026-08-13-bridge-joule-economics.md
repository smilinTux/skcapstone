# Bridge joule economics: SKJoule integration for the agent-run execute bridge

Status: design only, no implementation. Companion to
`2026-08-13-skharness-execute-bridge-arch.md` (the bridge itself) and the cost
ledger (`skharness/src/skharness/autocode/autopilot_cost.py`).

Chef's directive: joules are the TRUE measure of cost, and the accounting
"should all be built into skharness". This doc decides how the bridge's runs
touch the JouleWallet, when mint happens, on what evidence, keyed how, and how
the cost ledger and the wallet relate.

## 0. Recommendation (one line)

**Option (c), phased: keep the ledger recording every run's true joule cost
(shipped); replace the bridge's wallet no-op with a settlement that uses the
REAL ratify verdict (mint and spend only when `rr.passed`, with
`score=rr.score` and the card's real priority), deduped by
`(card_id, commit_sha)` in a settlement journal. Mint-on-merge (d) stays a
deferred Phase J2 refinement, built as a PR-state reconcile sweep, only if
evidence shows gate-passed drafts getting rejected at human review.**

## 1. Ground truth (the code as it stands)

- `joules.settle(agent, task_ref, *, priority, score, usage, commit_sha, home)`
  (skharness `autocode/joules.py:139`): records real token cost via
  UsageTracker, mints `XPBridge.calculate_joules("task_complete",
  priority=_priority_bucket(p), quality=_quality_bucket(score))`, spends
  `round(cost_usd * 50)` joules capped at the wallet balance, never raises.
  Contract in the docstring: "MUST only be called on a twin-gate pass
  (verified work)."
- `twin_gate_passed` (engineering.py:59-70) is the SINGLE pinned predicate:
  `gr.score == 5 AND is_complete(notes) AND ci_status == "green" AND cov >=
  floor`. Both the crown-jewel Ralph loop and `ratify()` call it; they cannot
  drift.
- `ratify()` (ratify.py) runs the exact per-round grade internals (stage,
  diff, external CI verdict, diff coverage, `harness.grade`, the pinned
  predicate) over an existing worktree, side-effect-free. **A ratify PASS is a
  bona fide twin-gate pass**, not a lookalike.
- `EngineeringExecutor._settle_economics` (engineering.py:116) settles with
  **hardcoded `score=5`**. That is safe there only because it is reached
  exclusively behind a twin-gate pass, where the score IS 5. On a non-pass the
  engine **drops** the accrued usage without any wallet effect
  (engineering.py:419: "no mint on a non-pass"). So the implemented precedent
  is: the wallet sees only passed builds; failed builds' cost never touches
  it.
- `DirectExecutor` (direct.py): its `GateResult.passed` means only "harness
  said ok AND diff non-empty", with `score=None`, mode `direct`, explicitly
  "UNGATED". Yet `finalize` calls the inherited `_settle_economics` when
  `result.passed`, which mints at quality "excellent" (the hardcoded 5) for
  ungated work. This is a pre-existing over-mint wart, flagged in section 8.
- `AgentRunDirectExecutor` (agentrun_bridge.py:72) overrides
  `_settle_economics` to a no-op ("P1: no joule mint from ad-hoc agent-run
  builds"), precisely to avoid inheriting that wart. But `execute_dispatch`
  ALSO holds a real twin-gate verdict (`rr = ratify(...)`, with `rr.score` and
  `rr.passed`) that the settle path never sees today.
- The cost ledger (autopilot_cost.py): every bridge run appends `{ts, date,
  card_id, repo, tokens, cost_usd, joules=round(cost_usd*50), passed, pr}` to
  `~/.skcapstone/autopilot-cost/ledger.jsonl` (Syncthing-synced), with a
  daily-USD cap gate and alerts. `_joules()` is a unit conversion only; the
  ledger never touches the wallet.

## 2. Q1: ledger vs wallet separation

They are two different layers and should stay that way:

| | Cost ledger | JouleWallet |
|---|---|---|
| What | Telemetry: the true joule COST of every run, pass or fail | Economy: the settled P&L of VERIFIED work |
| Writes | Every run, unconditionally | Only on a twin-gate pass |
| Semantics | "what did we burn" | "what did we earn, net of what it cost" |
| Failure mode | append-only, best-effort, never gates a mint | mint requires proof (twin-gate verdict + proof_hash) |

The ledger is where "joules are the TRUE measure of cost" lives: it prices
every run in joules including failures, and the daily cap reads it. The wallet
is where verified value accrues. Ledger-only (the shipped state) is **correct
for cost visibility but incomplete for the economy**: a bridge run that passes
the real twin gate is verified work by the exact same predicate the
crown-jewel engine mints on, and leaving it unminted makes bridge work
economically invisible, which breaks the per-agent P&L Chef wants joules to
be. So: yes, bridge runs should also settle to the wallet, but only on the
ratify verdict (section 3), never on the direct-mode `gr.passed`.

The two layers stay decoupled in code: the ledger keeps zero wallet imports
(its `_joules()` docstring already promises this), and settlement writes its
own journal (section 6) that the overview CLI can join against the ledger.

## 3. Q2: mint timing (the core decision)

### Options considered

**(a) Keep the no-op.** Safe, but permanently wrong: bridge work that passes
the real twin gate earns nothing, so the joule economy under-reports exactly
the runs Chef is standing up. Acceptable only as the shipped P1 state, not as
the destination.

**(b) Spend-only on every run.** Economically coherent in isolation (cost is
real; net-negative-until-value-lands is a defensible model, and the wallet
already floors at 0 with the intended/actual split in `Economics`), but it
**diverges from the implemented precedent**: the crown-jewel engine drops
usage on a non-pass (engineering.py:419), so failed gated builds do not drain
the wallet today. Adopting spend-on-fail only for the bridge would make the
same failure cost the wallet in one engine and not the other. It also creates
retry pain: every re-dispatch of a flaky card drains again, and there is no
compensating credit primitive. The ledger already carries the true joule cost
of every run including failures, so the wallet does not need to double as the
cost record. **Rejected as a standalone answer.**

**(c) Settle with the REAL ratify verdict. RECOMMENDED.** `ratify()` applies
the pinned `twin_gate_passed` predicate (LLM 5/5 + COMPLETE token + external
CI green + diff-coverage floor). When `rr.passed` is True, settle's contract
("MUST only be called on a twin-gate pass") is satisfied to the letter, by the
same predicate the crown jewel mints on. The direct-mode "never verified"
stance is also respected: it is not the direct `gr` being trusted, it is the
independent grade over the diff. Passing `score=rr.score` instead of a
hardcoded 5 keeps the wiring honest (today a pass implies score 5, so the
minted amount is identical, but if the predicate ever changes the mint tracks
reality instead of a literal). Non-passes mint nothing and spend nothing,
matching the crown-jewel precedent; their cost is on the ledger.

**(d) Mint-on-merge.** The philosophically purest "verified value" moment for
a draft PR is the human merge. But it diverges from the existing economy: the
crown jewel mints at twin-gate pass, not at merge (for non-automerge repos its
PR also awaits a human, and it still settles in `finalize`). Choosing (d) for
the bridge while the crown jewel keeps (c)-semantics would make the two
engines value identical evidence differently. It also needs new machinery
(section 3.1). **Deferred to Phase J2**, triggered only by evidence: if the
ledger+settlement journal shows a meaningful rate of `rr.passed` drafts being
closed unmerged, the mint moves to merge time.

### Recommended behavior, exactly

Phases (numbered J* to avoid colliding with the bridge doc's P1/P2/P3):

**Phase J0 (shipped): ledger-only.** Unchanged.

**Phase J1 (this design): settle on the ratify verdict, in
`execute_dispatch`, after `finalize` succeeds.**

- Keep `AgentRunDirectExecutor._settle_economics` a **no-op forever**. This is
  a structural invariant worth its comment: the `finalize` path is fed the
  ungated `mode="direct"` result and must never mint from it. The mint call
  site must be the one place that holds `rr` in hand.
- In `execute_dispatch`, after `ex.finalize(item, gr)` and a non-empty
  `pr_url`:
  1. Build a `BuildUsage` from the captured run cost. Today the
     `run_task` wrapper captures only `{cost_usd, tokens}`; extend it to also
     capture `res.raw` when present so `BuildUsage.from_claude_json` can keep
     the input/output split for UsageTracker fidelity; otherwise fall back to
     `BuildUsage(output_tokens=tokens, cost_usd=cost_usd, turns=1)`.
  2. **Close the grade-cost gap**: `ratify` calls `harness.grade`, an LLM
     call whose cost is currently captured nowhere. Wrap `harness.grade` with
     the same capture closure (restore in `finally`) and fold its cost into
     BOTH the ledger row and the settled usage. The run's true joule cost
     includes the grade.
  3. If `rr.passed` (which today implies `rr.score == 5`, CI green, coverage
     floor met) AND the settlement journal has no row for this
     `(card_id, commit_sha)` (section 6):
     `joules.settle(agent, f"airun-{card_id}", priority=card.priority,
     score=rr.score, usage=usage, commit_sha=head_sha)`
     where `head_sha` is `ex._head_sha(wt)` after the commit. Append the
     settlement journal row. Surface `econ.summary()` as an activity line so
     the kanban card shows the P&L next to the grade.
  4. If `rr.passed` is False: **no wallet effect**. The PR still opens (the
     bridge's contract: a reviewable draft either way), the ledger row still
     records the cost, and the summary already says "twin gate not passed".
- Everything wallet-side stays best-effort inside the existing
  `never-raise` discipline: a settle failure must never turn a shipped draft
  PR into a refusal.

**Phase J2 (deferred, evidence-gated): mint-on-merge reconcile.** See 3.1.

### 3.1 The mint-on-merge hook, if J2 is ever adopted

There is no usable event hook today: the bridge's `_ActivityDigestShim`
swallows `queue_decision` by design (the kanban card in review IS the review
surface; the autopilot decision resolver never sees bridge work), and no merge
webhook infrastructure exists in skharness. The honest mechanism is a
**reconcile sweep**, living in skharness next to the ledger (a
`reconcile_merges()` in `autopilot_cost.py` or a small sibling module), run
from the same cron cadence as the runner:

- Scan settlement-journal rows in state `pending` (J2 changes J1's step 3 to
  record the ratify verdict as `mintable-pending` instead of minting).
- For each, `gh pr view <pr> --json state,mergedAt,mergeCommit` in the repo's
  path from `cfg.repo_map`.
- `MERGED`: call `settle()` with the stored priority/score/usage, proof
  keyed on the **merge commit**, mark the row `settled`.
- `CLOSED` unmerged: mark `rejected`, no mint (cost stays on the ledger; no
  clawback primitive exists and none is needed since nothing was minted).
- Idempotent by row state; safe to re-run.

Do not build this until the settlement journal shows gate-passed drafts
actually getting rejected. Every J2 row is machinery plus a gh-auth dependency
the bridge does not otherwise have.

## 4. Q3: priority and score inputs

- **priority**: the folded card's real priority (`card.priority`, present on
  every `CardStore.fold` result, default "medium"). The bridge should put it
  into `item.payload["priority"]` when constructing the WorkItem (today the
  payload omits it) so the same value flows to settle, the ledger row
  (optional field), and any future journal reader. `_priority_bucket` already
  normalizes junk to "medium".
- **score**: `rr.score` from the ratify GateResult. Never the direct result's
  score (always `None`) and never a literal 5. `_quality_bucket` maps it; on a
  pass it is 5 today, and the honest wiring is future-proof.

## 5. Q4: agent identity for the wallet

Charge/credit **`context["agent"]`** (the requesting agent from the R1 seam,
e.g. "lumina"), falling back to the `_agent()` chain only when absent. Do NOT
use `EngineeringExecutor._agent()`'s environment lookup as the primary source:
the runner on noroc2027 executes under its own `SKAGENT`, and env-derived
identity would book every fleet agent's work onto the runner node's wallet.
The economy is a per-agent P&L; the card's requesting agent is whose work it
is.

Fleet accounting: `JouleWallet(agent, home=None)` resolves through the shared
root to `agents/<agent>/`, which is Syncthing-synced, so a settle executed on
noroc2027 lands in the right agent's fleet-wide wallet. The residual risk is
concurrent wallet writes for the same agent from two nodes (Syncthing file
conflict). Accepted for now with the same justification as the bridge doc's
R5: the runner processes runs sequentially (`run_once`), the autopilot engine
is disabled, and settlement is best-effort by contract. Optionally add a
`node` field to the ledger row for attribution of where cost was incurred; the
wallet stays agent-keyed only.

## 6. Q5: idempotency and double-count keys

Three distinct records, three distinct keys:

1. **Ledger** (`ledger.jsonl`): one row per RUN, append-only. A retry is a
   new run that really burned new tokens, so a second row is correct, not a
   double-count. Add a `run_id` field equal to the bridge's own journal handle
   stamp (`airun-<card_id>-<YYYYmmddTHHMMSSZ>`) so rows join cleanly against
   the run journal and the settlement journal.
2. **Settlement journal** (new, `~/.skcapstone/autopilot-cost/
   settlements.jsonl`, same dir and same defensive-append discipline as the
   ledger): one row per WALLET settlement:
   `{ts, run_id, card_id, repo, agent, commit_sha, pr, score, priority,
   minted, spent_joules, balance_after, state}`. The dedupe key is
   `(card_id, commit_sha)`: check before mint, append after. This is what
   makes settle-after-finalize idempotent against a crashed-and-replayed
   dispatch of the same commit.
3. **Wallet transactions**: `settle()` already stamps
   `proof_hash = XPBridge.compute_proof_hash(commit_sha)` on both the mint
   and the spend, so `skcapstone joule history` is independently auditable
   back to the commit. The wallet itself does not dedupe by proof hash; the
   settlement journal is the guard in front of it.

Residual risk, named: a full re-dispatch of the same card creates a NEW
commit sha (fresh worktree, fresh commit), so sha-keyed dedupe cannot catch a
semantic duplicate; the same card could mint twice via two passing runs. J1
hardening rule (cheap, journal-local): refuse a second mint for a `card_id`
that already has a settlement row whose PR is not closed-unmerged. This needs
no gh call at settle time if the rule is simply "one settlement per card_id
until an operator clears it"; start with that and loosen only if real re-run
workflows demand it.

The double-count question between ledger and wallet dissolves once their
meanings are kept distinct (section 2): the ledger's `joules` column is cost
telemetry, the wallet's spend is the settled cost of passed work. Summing
"ledger joules + wallet spend" is a category error, not a bug; the overview
CLI should present them as "burned (all runs)" vs "settled P&L (passed
runs)", joined on `run_id`.

## 7. Q6: alignment with the autopilot economic model

Consistent by construction: mint only behind `twin_gate_passed` (the same
pinned predicate, via ratify), spend the same `cost_usd * DEFAULT_JOULE_PER_USD`,
no wallet effect on a non-pass (matching engineering.py:419), per-agent
wallet, proof-hashed transactions visible in `skcapstone joule`. The bridge
becomes economically indistinguishable from the crown jewel for the wallet's
purposes, differing only in that its value ships as a draft PR instead of a
merge.

Divergences to flag (follow-up cards, out of this design's scope):

1. **DirectExecutor over-mints today.** `direct.py:89` settles on the ungated
   `result.passed` with the inherited hardcoded `score=5`, minting
   "excellent"-quality joules for single-round unreviewed work. The bridge's
   no-op override exists to dodge exactly this. The engine-side fix (either
   stop settling in pure direct mode, or ratify-then-settle as this doc does
   for the bridge) should be its own card; until then the bridge must not
   regress to the inherited path, which the permanent no-op guarantees.
2. **`_quality_bucket`'s docstring** says "only a twin-gate PASS (score 5)
   reaches settle()". With `score=rr.score` wiring that stays true in
   practice; update the docstring to name ratify as a legitimate caller.
3. **Caps are USD-denominated** (`cfg.caps.max_usd_per_day`) while Chef's
   direction is joules-as-true-measure. `autopilot_cost.summary` already
   surfaces `cap_joules`; a later config follow-up can flip the cap's unit to
   joules with USD derived, without touching this design.
4. **`skcapstone joule` CLI** shows wallet balance/history but not the burn
   ledger. A future `joule overview` join (wallet P&L next to ledger burn,
   per agent per repo) is the single pane Chef will actually read; the
   `run_id` key in section 6 is what makes it a join instead of a guess.

## 8. Summary of exact behavior changes (J1)

| Piece | Today | J1 |
|---|---|---|
| `AgentRunDirectExecutor._settle_economics` | no-op | no-op, unchanged, documented as permanent |
| `execute_dispatch` cost capture | wraps `run_task` only | also wraps `harness.grade`; also captures `res.raw` |
| Wallet settle | never | after finalize + PR, iff `rr.passed`, `score=rr.score`, `priority=card.priority`, `agent=context["agent"]`, `commit_sha=head_sha`, deduped by settlement journal |
| Ledger row | `{ts, date, card_id, repo, tokens, cost_usd, joules, passed, pr}` | plus `run_id` (and optionally `node`, `priority`) |
| New file | none | `settlements.jsonl` beside `ledger.jsonl` |
| Non-pass runs | ledger only | ledger only, unchanged |
| Mint-on-merge | n/a | deferred J2: reconcile sweep over pending settlements via `gh pr view`, evidence-gated |

No em or en dashes appear in this document (SKWorld hard rule).
