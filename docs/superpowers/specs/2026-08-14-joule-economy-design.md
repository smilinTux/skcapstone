# The Joule Economy: Work Grading, Energy Metering, and an Agent Labor Market

**Status:** Design approved by Chef 2026-08-14. Implementation not started.
**Scope:** Cross-repo (skgateway, skcoord, skcapstone, skharness, plus one new component).
**Home:** This spec lives in skcapstone. Paths below are repo-qualified (`skgateway/src/...`, `skcapstone/src/...`) and independent of where each repo is checked out.
**Companion:** https://claude.ai/code/artifact/ca020ab0-90b2-40ca-88a3-115d98be22a0

---

## 0. Decisions locked

These were settled with Chef on 2026-08-14 and are not open for re-litigation during
implementation. Changing one means coming back here first.

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Two axes**: `size` (reasoning difficulty) and `risk` (blast radius). `model_class = max(size, risk)`. | A single blended axis cannot express "easy work, dangerous consequences", which is 17% of the golden set. |
| D2 | **Hard floor, soft ceiling.** Below the class is refused. Above it is allowed but requires a written `escalation_reason` and the energy overage is debited. | Escalation reasons become the training data that corrects a bad rubric. A hard cap turns every misgrade into Chef's inbox. |
| D3 | Grade lives in **`meta.grade`**, optional, never a required top-level field. | `load_tasks` silently skips cards that fail validation. A required field would quietly empty a board of ~4,600 cards and look like a display bug. |
| D4 | **Joules are real physical energy**, not the current USD-derived unit. Three bases, always labeled. | An economy that silently mixes measured and estimated numbers lies to its operator. |
| D5 | **Pull market**: workers claim work; work is not assigned to workers. | Chef's call. It also forces D6. |
| D6 | **Grader-of-record is never the executor.** | In a pull market the grade *is* the job listing. A worker that writes its own listing posts itself an easy job with a fat bounty. |
| D7 | **Energy treasury** funded by a real daily budget. No minting from nothing. | Scarcity is what makes a saved joule mean anything, and it doubles as a runaway-cost brake. |
| D8 | **Design for untrusted workers from day one**, open the market later. | Retrofitting redaction, verification, and settlement later would touch the card schema and every worker. |
| D9 | **P0 (the meter) ships before everything else, in shadow.** | Every number in the economy is fiction until the meter exists and has been negative-controlled. |

---

## 1. Problem

Three problems, one shape.

**1.1 Every card is the same size to the system.** A coordination `Task` carries exactly one
judgment field: `priority`. Priority is urgency. It is not difficulty and it is not danger.
So a human or decomposer who already knows how hard a piece of work is has nowhere to write
it down, and every downstream consumer has to re-derive it from prompt text on every call.

**1.2 Nothing knows what work costs.** Nothing anywhere in SKWorld measures energy: zero hits
for joule, watt, or energy in the gateway source. There is no per-card cost attribution, no
estimate, and therefore no variance. Worse, the USD accounting that appears to exist does not
actually run. The gateway has `token_usage` and `cost_log` tables and never writes to them from
the live path (see 4.5.1), so the cost-visibility layer reads as present and is hollow.

**1.3 The wallet rewards activity, not thrift.** `_mint_joules_for_task` mints 25 to 500 J on
every `coord complete` with no cost debit at all. Balances only rise. Completing many trivial
cards is the dominant strategy, and adding a "joules saved" bonus on top would pay a premium
into that leak.

### 1.4 The problem this design found on its own

While building a throwaway meter to validate P0, we discovered that `sk-default` resolves to
backend `ornith-big` at `http://100.81.238.58:11436` (chiap08), which is **unreachable**, so
requests silently fail over to `openai/gpt-oss-20b`, a free remote model. Gateway counters at
discovery: `nvidia=180, chiap08-ornith=31, anthropic=2, local=0`. The local RTX 5060 Ti was up
and healthy the entire time and had served **zero** gateway requests.

No alert fired. No gate went red. `/status` reported every backend `up`, because failover
works. **A fallback that succeeds is indistinguishable from a healthy system until you measure
what it costs.** This is the same failure family as `status-signals-certify-less-than-they-appear`
and it is the strongest available argument for D9.

Tracked separately in GTD (source-ref `joule-economy-2026-08-14`); it is a live fix, not part
of this design.

---

## 2. What already exists

The new construction here is narrow. Most components exist and are merely disconnected.

| Capability | Status | Where |
|---|---|---|
| Difficulty classification | **exists** | `skgateway/src/classifiers/difficulty.mjs` scores `sk-auto` requests; `empirical.mjs` re-routes on observed quality |
| Model-class indirection | **exists** | Registry roles (`ornith-tiny`, `sk-default`, `sk-heavy`, `sk-vision`, ...), capability ranker `rank.mjs`, live `GET /admin/models/rank` |
| Sovereignty tiers | **exists** | `local` / `free-remote` / `paid-cloud` ladder in `rank.mjs` |
| Agent wallets | **exists, live** | `skcapstone/src/skcapstone/skjoule.py`; 19 wallets, 4,042 transaction lines total (2,054 of them lumina's). Entirely untested |
| Pull-based claiming | **exists** | `Board.claim_task` has **no capability check**; any agent can already claim any card. Plus `unblocked_task_ids()` and `auction.py` |
| Node placement scheduler | **exists** | `skcapstone/src/skcapstone/fleet/scheduler.py`, taints and tolerations, `sknoded` heartbeats, admission |
| Work verification | **built, disabled** | autocode sandbox → grade → twin-gate → merge, 846 test functions |
| pi + skgateway harness | **wired, unscheduled** | `~/.skcapstone/config/autopilot-pi.yaml`: `harness: pi`, `ornith-1.0-35b`, gateway base_url, `enabled: true`. Nothing schedules it. |
| Grade on the card | **absent** | `Task` has only `priority` |
| Energy measurement | **absent** | zero hits for joule/watt/energy in gateway source |
| Budget / estimate / variance | **absent** | only global `max_usd_per_day` caps |
| Live cost recording | **broken** | `recordResponse` is never called; `token_usage` and `cost_log` receive zero live rows (4.5.1) |

---

## 3. Layer 1: The Work Grade

### 3.1 Size, reasoning difficulty

| Value | Definition | Examples from the golden set |
|---|---|---|
| **S** | Mechanical and deterministic. Verifiable by inspection. No judgment calls. | run `npm install`; swap broken URLs; a pure `percent()` helper |
| **M** | Bounded implementation following an established pattern. Judgment inside a well-worn groove. | add an endpoint like the last one; tests for an existing function; a bug with a clean repro |
| **L** | Design within one subsystem. Requires holding several components in mind and choosing between approaches. | restart/watchdog escalation; drift detection across two nodes; a new feature in a live service |
| **XL** | Architecture. Changes contracts others depend on, spans repos, or has no pattern to follow. | wiring calling end-to-end across skchat and skcomms; a streaming voice pipeline; an umbrella epic |

### 3.2 Risk, blast radius

| Value | Definition | Examples |
|---|---|---|
| **LOW** | Isolated or throwaway. A wrong answer is noticed immediately and costs nothing. | scratch analysis, docs, local experiments |
| **MED** | Changes one service's behavior. Degradation shows within a day; a revert fixes it. | app feature, cron output, a dashboard view |
| **HIGH** | Touches auth, data integrity, secrets, or the fleet control plane. Failure can be silent and costly. | PDP policy, capauth, sync thresholds, admission path |
| **CRIT** | Irreversible or safety-relevant. Never fully automated; always routed to Chef. | destroys data, rotates keys, sends outbound comms, changes what agents may do |

### 3.2.1 The two axes must not share labels

**`risk` never uses S/M/L/XL.** This is a hard rule and it has already been
broken once in a downstream list, which is why it is written here explicitly.

The axes exist so a reader can tell them apart. `size: M, risk: crit` says
ordinary work with dangerous consequences, and the correct response is a human,
not a bigger model. `size: XL, risk: XL` says nothing at all: you cannot tell
which value came from which axis, and the single most valuable signal the
grading system produces (easy work that is dangerous, 17% of the golden set)
becomes unreadable.

There is no mechanical benefit to shared labels either. `max(size, risk)`
compares RANKS, and the ranks already align one to one. Shared labels buy
nothing and cost grep-ability: `risk: crit` is unambiguous, `risk: XL` collides
with every size value in the codebase.

`crit` also carries a rule that `XL` does not: **CRIT always routes to Chef**,
regardless of grader confidence. That instruction lives in the label.

A machine-readable copy of all three vocabularies, with ranks, definitions and
worked examples, is committed beside this spec as
`joule-grade-vocabulary.json`. Consume that file rather than retyping the
enums; it is the single source both this epic and the gateway's model-matching
spec resolve against.

### 3.3 The rule

```
size_rank = {S:0, M:1, L:2, XL:3}
risk_rank = {LOW:0, MED:1, HIGH:2, CRIT:3}
model_class = CLASS[max(size_rank[size], risk_rank[risk])]   # -> S|M|L|XL
```

Worked examples:

```
S  / LOW   -> S     trivial and harmless
S  / HIGH  -> L     one threshold value, but it guards against disk-full corruption
M  / CRIT  -> XL    a subprocess wrapper that hands an agent fleet-wide ansible
L  / LOW   -> L     hard thinking, throwaway output
XL / CRIT  -> XL    and routed to Chef regardless of grade
```

**Floor is hard.** A worker whose class is below `model_class` may not claim the card.
**Ceiling is soft.** Exceeding `model_class` is permitted, but the worker must write
`escalation_reason` onto the card and the energy overage is debited from its wallet.

`risk = CRIT` always routes to Chef regardless of confidence. So does any grade with
`confidence < 0.6`.

### 3.4 Schema

Nested under the existing extensible `meta` dict, following the `meta.autopilot` precedent
exactly. Optional, with defaults, so existing cards are untouched.

```jsonc
"meta": {
  "autopilot": { /* unchanged */ },
  "grade": {
    "size": "M",                       // S|M|L|XL   reasoning difficulty
    "risk": "high",                    // low|med|high|crit   blast radius
    "sensitivity": "internal",         // public|internal|secret   data exposure
    "model_class": "L",                // derived, stored for queryability
    "joule_estimate": 42000,           // expected marginal joules
    "joule_bounty": 46200,             // estimate + margin; what the worker competes for
    "graded_by": "assessor@noroc2027",
    "grader_model": "ornith-1.0-35b",
    "rubric_version": 1,
    "confidence": 0.82,
    "pool": "private",                 // private|public
    "graded_at": "2026-08-14T20:00:00Z"
  }
}
```

### 3.4.1 Sensitivity is a third field, not a third rank

`size` and `risk` decide how much model the work needs. Neither says anything
about where the work may be sent, and that is a real gap: **blast radius and
confidentiality are different axes.** A docs card written against the private
legal corpus is `risk: LOW` and must still never reach a free remote provider.
Conversely a card that deletes stale files in a public repo is `risk: CRIT` on
entirely public data.

So the grade carries `sensitivity` alongside them:

| Value | Meaning |
|---|---|
| `public` | nothing in the payload that could not be posted publicly |
| `internal` | fleet or business context, code, ops detail. The default for agent traffic |
| `secret` | credentials, keys, private corpora (legal/medical), soul or memory content, anything under seal |

It does NOT feed `model_class`. `model_class` stays `max(size, risk)` exactly as
D1 defines. Sensitivity resolves separately, through a policy map, to a ceiling
on which providers are eligible at all, so a request can fail closed rather than
silently cross into a less trusted zone.

**Why it lives inside `meta.grade` rather than beside it.** The grade block is
everything the grader-of-record decided about this card, and sensitivity is a
grading judgment made at the same moment, by the same actor, from the same card
text. It needs the same provenance the other fields already carry
(`graded_by`, `rubric_version`, `confidence`). A sibling `meta.sensitivity`
would mean two writers, two provenance trails, and no way to arbitrate when the
grade and the sibling disagree.

This answers **Q3** of the companion gateway spec,
`skgateway/docs/specs/2026-08-14-model-metadata-risk-job-matching-arch.md`,
which consumes this grade to match work against models. That spec owns the
model side (`size_class`, `trust_zone`, `latency_class`) and the matching gate;
this one owns the job side and the vocabulary. Neither reinvents the other's
half: the enums, the `max()` rule, and the floor/ceiling semantics are defined
here and consumed there verbatim.

### 3.5 The grader

The grader-of-record is a pass that runs **before a card becomes claimable**. It extends the
existing `phase0_assess`, which already computes a `concreteness` score, rather than
introducing a second traversal of the board.

**The grader always runs at class M**, fixed, regardless of what it is grading. This resolves
the chicken-and-egg cleanly: classification against a written rubric is an M task whether the
subject is an S typo fix or an XL migration. One M call per card, once.

### 3.6 The rubric must be executable

A rubric that lives only as English drifts; S creeps toward L over months and nobody notices.
The rubric therefore ships with a **golden set** (Appendix A): 42 completed cards graded by
hand. The grader is scored against it. A `rubric_version` bump **requires** re-running the
golden set and publishing the delta. Target agreement: 85% exact class match, 100% within one
class.

---

## 4. Layer 2: The Meter

### 4.1 What the hardware can actually do

Measured on 2026-08-14, not assumed:

- **RTX 5060 Ti on .100** (driver 580.173.02): `power.draw` works, idle 8.85 to 8.96 W against
  a 180 W limit. **`total_energy_consumption` is not a valid query field.** There is no
  cumulative hardware counter.
- **CPU energy is unmeasurable fleet-wide.** No `intel-rapl` on .100; the primary box is a QEMU
  virtual CPU with powercap not exposed.
- **Cloud is unmeasurable** by definition.

Therefore: **energy is integrated from sampled power**, and the three bases below are not a
nicety, they are the only honest representation.

### 4.2 Three bases

| Basis | Applies to | Method | Trust |
|---|---|---|---|
| `measured_gpu` | .100 and future GPU nodes | integrate sampled `power.draw`, minus idle baseline | real physics |
| `imputed_local` | CPU-only local work | J/token coefficient, calibrated against a metered proxy | estimate |
| `imputed_cloud` | Opus, Sonnet, NIM, OpenRouter, Kimi | J/token per model family, each coefficient carrying a cited source | estimate, wide error |

`energy_basis` is recorded on **every** settlement. Aggregates that mix bases must report the
mix, never a single blended number presented as measured.

### 4.3 Marginal, not absolute

We charge `(P_busy - P_idle) x t`. Idle draw is a fleet overhead paid whether or not any card
runs; billing it to whoever happened to be executing makes short calls noise-dominated. Idle is
tracked as a separate fleet line item so it stays visible rather than disappearing.

### 4.4 `skmeter`, a new component

A small sidecar on each GPU node. It exists because the GPU has no counter, so we synthesize
one.

- Samples `nvidia-smi --query-gpu=power.draw` at ~5 Hz (validated rate; 10 Hz is a tuning
  option, not a requirement).
- Maintains a **monotonic joule counter** in memory with periodic disk checkpoints, so a
  restart does not silently reset consumption to zero.
- Re-measures the idle baseline nightly, and exposes it.
- Serves `GET /energy` returning `{counter_j, watts_now, idle_baseline_w, device, node, ts, samples_n}`.

Counter semantics matter: the gateway reads the counter **before dispatch** and **after
response** and takes the delta. This is more robust than per-request sampling coordination and
it degrades gracefully, because a missed read produces a gap, not a wrong number.

### 4.5 Gateway integration

The gateway is the only chokepoint that sees every inference and knows which backend served it,
which is why the meter lives there rather than in 35 individual call sites.

- Read the sidecar counter before dispatch and after completion; write the delta to a new
  `energy_log` table in the existing `metrics.db`, alongside (never replacing) `cost_log`.
- Attribute to a card via a new `x-sk-card-id` request header, joining the existing `x-sk-*`
  header family.
- **Return energy to the caller.** Response headers `x-sk-energy-joules`,
  `x-sk-energy-basis`, `x-sk-energy-node`, plus an `energy` block inside the usage object.
  No client can react to what it spends today, because nothing is returned to it.
- Config: a new `energy:` block in `skgateway.yaml` holding meter endpoints per backend and the
  imputation coefficient table, loaded through the existing hot-reload path.

Precise landing points are in Appendix B. Four of them contradict the obvious guess and would
each cost a day if discovered during implementation:

**`src/proxy/core.mjs` is dead code on the live path.** `index.mjs` imports it but uses only
`buildConfig`; the `proxyConfig` it builds is never used. The live path is `routeAndSend` in
`src/proxy/router.mjs:1452`. Meter reads belong **per attempt** inside the candidate loop
(around `router.mjs:1793-1831`), not once per request, so that a failover attempt is attributed
to the backend that actually served it.

**Streaming is not a separate path.** `sendUpstream` (`upstream.mjs:101`) always buffers the
full response, so a client `stream:true` request is buffered whole and relayed verbatim. The
after-read point is identical for both. The real streaming problem is different and narrower:
`extractUsage` (`router.mjs:281`) returns `{}` for SSE bodies because `JSON.parse` fails, so
**token counts are missing for streamed responses** and imputation must parse the final `data:`
chunk or fall back to a byte heuristic.

**Backends have no node identity.** `BackendConfig` (`router.mjs:142-155`) carries no hostname
or node concept; the only locality signal is `Backend.url`. The `energy:` config block must
therefore map backend id (including synthetic `reg:*` ids from `getRegBackend`) or URL host to
a meter endpoint.

**`x-sk-card-id` must be stripped before forwarding upstream.** `routeAndSend` forwards client
headers and strips only host/connection/keep-alive/accept-encoding
(`router.mjs:1744-1756`). Leaking internal card ids to NVIDIA or OpenRouter is a small but real
disclosure.

### 4.5.1 P0 must also repair the metrics wiring it depends on

`index.mjs:1411-1422` calls `metrics.recordRequest({path, method, duration, status, agent_id,
model, backend})` once, after the response, with a shape that does not match the collector's
signature (snake vs camel case, no `sessionId`, no response object). **`recordResponse` is never
called on the live path at all.**

The consequence: `token_usage` and `cost_log` receive **zero rows** from live traffic. The
tables exist, the DDL runs, the queries work, and the data is not there. This means
`skcapstone autopilot-cost` and the skdashboard Economy view have been reading a ledger the
running gateway never writes to. Combined with the separate finding that the autopilot cost
ledger's 87 rows are all leaked test fixtures, **the entire cost-visibility layer is currently
hollow.**

P0 therefore has a prerequisite that is not optional: wire `recordRequest` before dispatch and
`recordResponse` after completion with the correct shapes. Energy accounting must not be bolted
onto a path that was never recording anything. This also means any pre-P0 baseline drawn from
`cost_log` is meaningless and must not be used as a comparison.

Note also that `metrics.db` has **no migration mechanism**: the DDL at `collector.mjs:190-255`
is pure `CREATE TABLE IF NOT EXISTS` executed on boot. Adding `energy_log` is therefore easy,
and altering any existing table has no supported path. Design `energy_log` to be right the
first time.

### 4.6 Concurrency

If two requests share a GPU, marginal energy splits by output tokens generated in the overlap
window. Accurate at low concurrency, approximate at high. Every row records `concurrency_n` so
calibration can filter for clean single-tenant measurements. This limitation is documented, not
hidden.

### 4.7 Negative control (blocking requirement)

The meter is not trusted until it has been checked against a known load:

1. Run one fixed prompt 100 times. Joules-per-run variance must fall inside a stated threshold.
2. Confirm the integral matches `mean_watts x wall_time` computed independently.
3. **Negative control:** issue a request that routes to a cloud backend and confirm the local
   meter reports `0` marginal joules and basis `imputed_cloud`, not a spurious measurement.

Item 3 is exactly the check that surfaced the `sk-default` failover. Given how many green gates
in this fleet have certified less than they appeared to, this validation gates P0 completion.

### 4.8 Reference measurement

The first real measurement in SKWorld, for regression:

```
node .100, RTX 5060 Ti, ornith-1.0-9b via llama-server :8082
600 output tokens, 8.37 s wall
idle 8.96 W | mean 99.12 W | peak 140.77 W | 95 samples over 19.0 s
TOTAL 1,883 J   MARGINAL 1,713 J   ->  2.85 J per output token
1M local tokens = 2.85 MJ = 0.79 kWh = ~$0.24 at CT rates
```

---

## 5. Layer 3: The Market

### 5.1 Worker contract

```jsonc
{
  "worker_id":       "pi-ornith@100",
  "max_model_class": "M",
  "capabilities":    ["repo:skchat", "lang:python", "sandbox:docker"],
  "trust":           "fleet",          // fleet | foreign
  "meter":           "measured_gpu",   // or imputed_*
  "pubkey":          "<capauth identity>"
}
```

### 5.2 Eligibility

A worker may claim a card if and only if:

1. `worker.max_model_class >= card.meta.grade.model_class`
2. worker capabilities cover the card's requirements
3. `card.meta.grade.pool` is visible to `worker.trust`
4. the card is unblocked (existing `unblocked_task_ids` logic, unchanged)

This reuses the existing taint/toleration scheduler for node placement. **Do not build a second
scheduler.**

### 5.3 Three rules that keep the market honest

- **Escrow on claim.** The bounty moves from treasury to escrow bound to (card, worker). Claims
  carry a TTL; the existing `release_stale_claims` returns abandoned escrow to the treasury and
  records the abandonment against worker reputation.
- **Claim limits.** A worker holds at most N concurrent claims, N set by its class, so one eager
  worker cannot vacuum the board.
- **Bounties age upward.** An unclaimed card's bounty grows on a schedule until someone finds it
  worth taking. Without this, workers take only S cards and XL work starves forever. The market
  rebalances itself without a scheduler deciding what is important.

---

## 6. Layer 4: The Economy

### 6.1 Settlement

```
earned = bounty - actual_joules - escalation_overage - rework_penalty
```

Beat the estimate, keep the difference. Blow it, eat it. **There is deliberately no completion
bonus**: the margin is the reward, and a second reward channel is a second thing to farm.

`actual_joules` is the sum of metered energy for all inference attributed to the card via
`x-sk-card-id`. The worker reports nothing about its own consumption.

### 6.2 Monetary policy

The treasury is a finite daily joule allowance. Bounties escrow against it, unspent escrow
returns to it, and when it empties the board pauses until the next period.

**The electric bill cannot set this number.** Household load is roughly 100 kWh/day; at the
measured 2.85 J/token, a million local tokens per day is under 1% of it. Any bill-derived
figure would never bind, making it a scoreboard rather than a brake. What the bill legitimately
sets is the **price** of a joule (~$0.27 to $0.30/kWh in CT), so the ledger can be denominated
in real money.

**Starting treasury: 10 MJ/day** (2.78 kWh, ~$0.83/day, ~$25/month, under 3% of household load),
buying roughly 3.5M local tokens/day. This is a dial, and P0's shadow data is what makes it
informed. It is not a physics constant and should be revisited after two weeks of measurement.

### 6.3 Closing the free-mint leak

It is not one leak. There are **three independent mint paths**, and they are not mutually
exclusive:

1. **`Board.complete_task` → `_mint_joules_for_task`** (`coordination.py:862`, fn `:944-1000`).
   Fires on every `coord complete`. It does **not check that the task exists, that it was
   claimed, or that it was already completed**. If the task file is missing it mints anyway with
   defaults (`:960-966`). There is no idempotency, so re-completing a card mints again. Failures
   are swallowed (`:998-1000`).
2. **`settle()`** (`skharness/autocode/joules.py:139-217`) mints on twin-gate pass and then
   debits USD-derived joules. When a card goes through autocode *and* is completed on the board,
   **both paths mint for the same work**.
3. **`skcapstone joule mint`** (`cli/joule_cmd.py:251`) is an unauthenticated free mint.

Quality is also self-declared: `auto_tokenize_task` (`skjoule.py:765-771`) infers the quality
multiplier from card *tags*, so tagging your own card `excellent` multiplies the payout by 3.

Under the treasury, path 1 becomes an **escrow release** rather than a mint, path 2 settles
against that same escrow instead of minting independently, and path 3 requires the treasury as
counterparty. Cards completed outside the market (Chef by hand, interactive sessions) settle
through a `manual` path that pays nothing.

### 6.3.1 Wallet migration is the riskiest step in the whole design

Live state: **19 wallets, 4,042 transaction lines.** Three properties make this dangerous:

- **Snapshot writes are not atomic.** `_persist_unlocked` (`skjoule.py:421-445`) uses plain
  `write_text`, unlike the coordination layer which uses `atomic_write_text`.
- **A corrupt `joules.json` silently resets the balance to zero.** `_load_or_create_snapshot`
  (`:392-415`) logs a warning and creates a fresh zero snapshot. A crash mid-write during
  migration therefore destroys a balance and reports success.
- **There is no export, import, or backup mechanism**, and **no test anywhere references
  `JouleWallet` or `skjoule`.** The wallet is entirely untested.

The `transactions.jsonl` append-only log is the only replayable source of truth, which makes it
the migration's foundation rather than the snapshot.

Migration procedure, in order, no steps skipped:

1. **Snapshot** every `wallet/` directory to a dated backup outside the agent tree.
2. **Freeze** minting: kill switch on all three paths before touching anything.
3. **Quarantine** the 87 leaked test-fixture rows in the autopilot ledger (`repo: skrender`,
   `card: task-abc`, `cost_usd: 0.0`). Quarantine, never delete, or the replay is wrong.
4. **Replay** balances from `transactions.jsonl` and compare against the current `joules.json`.
   Any wallet where replay and snapshot disagree is a pre-existing corruption and must be
   reported to Chef, not silently reconciled.
5. **Reconcile** and cut over.
6. **Write tests first.** Given zero existing coverage, the migration script's tests are the
   first tests the wallet has ever had. Make `_persist_unlocked` atomic as part of this work.

### 6.4 Anti-gaming rules

These are load-bearing. Removing any one of them makes the economy theater.

1. **The estimator never earns from the estimate.** Grader sets the bounty; worker earns against
   it. Any system where one actor does both converges on inflated estimates within days.
2. **Payout requires verification, never self-report.** Settlement gates on the twin-gate, which
   is already built and merely switched off.
3. **Energy is metered, not declared.** Workers the gateway cannot meter get imputed values, so
   lying about power draw earns nothing.
4. **Rework claws back.** A card reopened after completion debits the original worker, pricing
   the difference between finishing and appearing to finish.

### 6.5 Reputation

Tracked from day one because it costs almost nothing now and is a hard prerequisite for L5:
per-worker completion rate, rework rate, escalation rate, and estimate accuracy.

---

## 7. Layer 5: The Open Market

Donated outside compute crunching sanitized cards. Every prerequisite is hard and none may be
skipped.

| Prerequisite | Why it is hard |
|---|---|
| **Card sanitization** | A `pool: public` card must pass a **fail-closed** redaction gate: no secrets, no internal hostnames, no tailscale addresses, no private repo paths. Given the 06-10 leak, SKGentis committed keys, and the skos secret still in public history, a linter warning is not sufficient. |
| **Machine-checkable acceptance** | A stranger's output is worthless unless verifiable without a human. Only cards whose acceptance criteria a machine can evaluate may go public. |
| **Their compute, not yours** | Foreign work never executes on the fleet. You receive a diff and verify it in your own sandbox. |
| **Sybil resistance** | capauth identity per worker; reputation gates class eligibility; a stake in escrow lost on verified-bad submissions. Newcomers get S cards only. |
| **Imputed energy only** | You cannot meter someone else's GPU, so foreign settlements price from the coefficient table and cannot be inflated. |

---

## 8. Where else this applies

The inventory found **19 call sites hardcoding a model** against 16 routing through the gateway.

| Site | Today | Under this design |
|---|---|---|
| skwhisper digest | hardcoded qwen3.6 :8082, 30-min loop | Highest-volume LLM consumer in the fleet and completely unmetered. Grade S, route it, measure it. |
| dream reflection 04:00 | hardcoded **cloud Kimi via NVIDIA** | A creative job on paid cloud nightly that nobody is measuring. Meter first, then decide. |
| mail 4C triage 06:10 | hardcoded ornith-9b, **bypasses the gateway** | Grade S and route it. Currently invisible to every control that exists. |
| wiki stub fill 04:45 | hardcoded 35B, 15 calls | Grade M, routed. |
| session to memory 2x/day | hardcoded haiku, comment says "fast + cheap" | That comment is already a size judgment. Make it a declared S. |
| GTD triage, order adapter, watchdog | already `sk-default` | Add grade and meter. Nearly free. |
| ITIL change validation | varies | The risk axis fits natively: a CRIT change draws an XL reviewer. |

### 8.1 Where it does NOT fit

**Interactive voice is latency-bound, not intelligence-bound.** It needs a fast model regardless
of how hard the question is, and a sizing axis that only knows difficulty will route it wrong
every time. Voice paths (`lumina-call.py`, `voice-llm-proxy.py`) stay **outside** the system in
phase 1. Revisit with data rather than speculatively bolting on a third `latency_class` axis.

---

## 9. Phasing

| Phase | Deliverable | Gate to exit |
|---|---|---|
| **P0** | **The meter, shadow only.** `skmeter` sidecar, gateway energy accounting, imputation table, `energy_log`. No behavior changes. | Negative control passes (4.7). Two weeks of data collected. |
| **P1** | **The grade, shadow only.** Rubric, golden set, `meta.grade`, grader in `phase0_assess`. Grade every card, route nothing. | Grader hits 85% exact / 100% within-one against the golden set. |
| **P2** | **Routing.** `sk-xl/l/m/s` roles in the registry; callers request a class. Ceiling enforcement with escalation records. | Escalation rate per class is stable and explainable. |
| **P3** | **Treasury and settlement.** Close the free-mint leak, migrate wallets, escrow bounties, variance reporting. | Wallet reconciliation is exact against the pre-migration snapshot. |
| **P4** | **The market.** Worker classes, pull claiming, bounty aging, reputation. | XL cards are not starving. |
| **P5** | **The open market.** Foreign workers, sanitization, stake and slashing. | Redaction gate has a passing negative control on a card containing a planted secret. |

**P0 first, and it pays off even if the rest is abandoned**, because knowing the fleet's real
energy cost is independently valuable. It has already returned one live finding before being
built properly.

---

## 10. Risks and things that will bite

| Risk | Mitigation |
|---|---|
| **A required schema field empties the board.** `load_tasks:219-231` silently skips invalid cards with only a warning; ~4,600 task files exist. | D3: `meta.grade` is optional with defaults. Add a test that loads a card with no `meta.grade` and asserts it still appears on the board. |
| **Wallet migration destroys a balance.** Snapshot writes are non-atomic and a corrupt snapshot silently resets to zero. 4,042 live transactions, zero existing tests. | Full procedure in 6.3.1. Replay from the JSONL log, not the snapshot. Make `_persist_unlocked` atomic as part of the work. |
| **Double-minting.** Three independent mint paths, and autocode + board completion both fire for the same card. | 6.3. Unify on escrow release; add idempotency keyed on card id. |
| **Building energy accounting on a metrics path that records nothing.** `recordResponse` is never called; `token_usage` and `cost_log` get zero live rows. | 4.5.1 makes repairing the wiring a P0 prerequisite. Do not draw a pre-P0 baseline from `cost_log`. |
| **Integrating into `core.mjs`, which is dead code.** | 4.5: the live path is `routeAndSend` in `router.mjs:1452`. Per-attempt, inside the candidate loop. |
| **Concurrent writers corrupt task files.** `_write_task_raw` has a documented single-writer precondition. | The grader must respect the same single-writer constraint autopilot does. Do not run graders on two nodes. |
| **The meter is trusted before it is validated.** | 4.7 is a blocking gate, including the cloud negative control. |
| **The rubric drifts.** S creeps toward L; everything gets more expensive and nobody notices. | Golden set + `rubric_version` + mandatory re-run on bump. Escalation reasons reviewed weekly. |
| **Estimate inflation.** | D6 plus anti-gaming rule 1. Structurally prevented, not policed. |
| **XL card starvation** in a pull market. | Bounty aging (5.3). |
| **Grading cost exceeds the savings.** One M call per card is not free. | Measure it in P1 shadow. If grading costs more than routing saves, the rubric is too expensive and should shrink toward heuristics with model fallback. |
| **`imputed_cloud` coefficients are guesses presented as data.** | Every coefficient carries a cited source and a review date. Aggregates report the basis mix. |
| **Concurrency skews attribution.** | Record `concurrency_n`; filter for clean measurements when calibrating. |
| **Voice paths get routed by size and become slow.** | Explicitly out of scope in phase 1 (8.1). |

---

## 11. Open questions

1. **Treasury value.** 10 MJ/day is a straw proposal, not a derivation. P0 data sets the real one.
2. **Whole-node metering.** No smart plug or PDU exists today. One energy-monitoring plug on
   .100 would upgrade whole-node draw from imputed to measured and is the highest-value hardware
   purchase for this project.
3. **Household kWh is unknown.** Eversource paperless billing ended in 2024 so no bill PDFs exist
   in email, and the portal did not open under automation (Playwright cannot attach to Chrome
   151). We have dollars ($902.33 dated 07/13/2026) but not kilowatt-hours. This blocks nothing:
   per 6.2 the bill sets the price of a joule, not the treasury.
4. **Cross-cluster inference.** `sk-default` pointed at chiap08, a `chi*` host, which by
   convention belongs to the other sovereign install. Whether the fleet should depend on it at
   all is a policy question outside this design.

---

## Appendix A: Golden set v1

42 cards sampled from 1,552 completed, stratified across security, infra, app, platform, docs,
test, and autopilot work, and across description depth. Machine-readable copy committed alongside
this spec as `2026-08-14-joule-economy-golden-set-v1.json`.

**Distribution:** 4 S, 18 M, 12 L, 8 XL.

**7 of 42 (17%) had their class raised by risk** above what difficulty alone would assign. This
is the empirical justification for D1.

| Card | Size | Risk | Class | Why risk lifted it |
|---|---|---|---|---|
| `78dcf8ae` | M | CRIT | **XL** | A simple subprocess wrapper that hands an agent arbitrary fleet-wide ansible execution |
| `57c4fdb1` | L | CRIT | **XL** | Uninstall wizard: deletes data, logs out of Tailscale, irreversible |
| `06e50405` | S | HIGH | **L** | One threshold value, but it is the guard preventing disk-full corruption |
| `9edc0b1e` | M | HIGH | **L** | Diagnosis done and wiring is mechanical, but it is an auth gate on guest routes |
| `1dc54376` | S | MED | **M** | A cron timer, except it overwrites the file that steers every agent |
| `9b8e2dac` | S | MED | **M** | Run one command, but it rewrites agent instructions |
| `ceae627c` | S | MED | **M** | Eight path strings. Critical *priority*, small *size*, moderate *risk* |

That last row is the clearest evidence the vocabulary does real work. `ceae627c` is filed
`critical` priority, and priority is the only judgment field cards carry today, so under the
current schema an agent has no way to learn that this urgent card is also small and ordinary.

**Anchors worth preserving across rubric versions:**

- `8e332618` (S): a pure `percent()` helper plus three tests. Already built by pi+ornith, which
  validates the sovereign harness premise directly.
- `ac52140e` (M): one pure function, fully specified acceptance, explicitly not a guardrail.
- `8ee5151c` (L): restart and watchdog escalation. This exact bug class caused the 13-hour outage.
- `da7c941c` (XL): wiring calling end-to-end across skchat and skcomms.

---

## Appendix B: Implementation landing points

Verified 2026-08-14 against the live trees. Line numbers will drift; the function names are the
durable handle.

### B.1 skgateway (`skcapstone-repos/skgateway`)

| What | Where |
|---|---|
| **Live request path** | `src/index.mjs:841` server → proxy branch `:1237` → routeRequest assembled `:1344-1362` → dispatch `:1364-1366` |
| **The dispatcher** | `routeAndSend(router, request, upstreamPath, method, clientHeaders, body, usePool, siem)`, `src/proxy/router.mjs:1452` |
| **Backend finally chosen** | candidate loop `router.mjs:1712`, destructure `:1713` |
| **Upstream issued** | `sendUpstream(...)` at `router.mjs:1803/1812/1815`; impl `src/proxy/upstream.mjs:101` (always buffers) |
| **Response complete, usage known** | `router.mjs:1831` latency, `:1833` recordOutcome, `extractUsage` `:281-294`, return `:1906` |
| **→ Meter read points** | per attempt, around `router.mjs:1793-1831` (pool slot acquired `:1765`, released `:1827`) |
| **Header forwarding / strip list** | `router.mjs:1744-1756`, add `x-sk-card-id` to the strip list |
| **Client response headers** | `index.mjs:1402-1407` (generic + buffered SSE), `:1396-1398` (Anthropic non-stream), `:1392-1393` (`SSEWriter.start`) |
| **`x-sk-*` parsing** | `index.mjs:1352-1361` (`x-sk-context`, `x-sk-service`, `x-sk-role`, `x-sk-require`), add `x-sk-card-id` here |
| **Metrics DDL** | `src/metrics/collector.mjs:190-255`, executed `db.exec(DDL)` at `:383`. No migrations; `CREATE TABLE IF NOT EXISTS` only |
| **Insert statements** | `collector.mjs:389-423`, batch `flushBatch:426-437`, `maybeFlush:444` (5s / 100 rows) |
| **Cost row assembly** | `recordResponse` `collector.mjs:738-753`, energy row goes beside it |
| **Pricing lookup pattern** | `getPricing(model)` `src/config.mjs:880-896`, model the energy coefficient lookup on this |
| **Broken wiring to fix** | `index.mjs:1411-1422` calls `recordRequest` with a mismatched shape; `recordResponse` never called |
| **Backend config shape** | `BackendConfig` `router.mjs:142-155`; synthetic ids via `getRegBackend` `:1227-1234` |
| **Config load + hot reload** | `loadConfig` `src/config.mjs:766`; SIGHUP `:773/:834`; applied `index.mjs:1451-1458`. Add `energy:` to `DEFAULTS:77` and `validate()` |
| **Test runner** | `node --test tests/*.test.mjs` (package.json:13) |
| **Test to model on** | `tests/siem-live-hook.test.mjs`, `startUpstream()` `:33` spins a stub upstream, calls `routeAndSend` directly, asserts a side channel. Add a stub meter server the same way |

### B.2 skcoord (`skcapstone-repos/skcoord/src/skcoord/coordination.py`)

| What | Where |
|---|---|
| **Task model** | `class Task(BaseModel)` `:96`; `meta: dict = Field(default_factory=dict)` `:114` |
| **The only mutation helper** | `_write_task_raw(task_id, mutate)` `:326`, raw dict, not the model, so `meta.*` survives; `atomic_write_text`. **Single-writer precondition** `:335-337` |
| **Writer precedent to copy** | `score_task` `:355-393` (`d.setdefault("meta",{}).setdefault("autopilot",{})`, idempotent replace); also `record_attempt` `:402-451`, `mark_decomposed` `:557-580` |
| **The silent-skip landmine** | `load_tasks` `:205-232`, skip at `:219-231` |
| **Claim eligibility** | `Board.claim_task` `:799-840`; current checks `:821-825`. **Worker-class check inserts between `:825` and `:827`** |
| **Unblocked set** | `unblocked_task_ids` `:582-601` |
| **Free mint** | `Board.complete_task` `:842-865`, mint call `:862`; `_mint_joules_for_task` `:944-1000`; `_PRIORITY_JOULE_MAP` `:936-941` |
| **Tests** | `skcoord/tests/`, pytest. `test_failure_memory.py:101-123` is the `meta.*` writer template |

### B.3 skcapstone (wallet + CLI)

| What | Where |
|---|---|
| **Wallet** | `src/skcapstone/skjoule.py:160`; `mint` `:210`, `spend` `:245`, `transfer` `:284` (sorted lock order `:312`) |
| **Models** | `Transaction` `:102`, `TransactionKind` `:59`, `WalletSnapshot` `:116` |
| **Persistence** | `_persist_unlocked` `:421-445` (**non-atomic `write_text`**); `_load_or_create_snapshot` `:392-415` (**corrupt → silent zero reset**) |
| **Auto-mint** | `JouleEngine.auto_tokenize_task` `:739-809`; self-declared quality from tags `:765-771`; `record_work` `:800` |
| **CLI** | `cli/coord.py` (`score:178` is the precedent for a grade flag); `cli/joule_cmd.py` (`mint:251` is the third leak) |
| **MCP** | `mcp_tools/coord_tools.py` (`coord_score:99`). No joule MCP tools exist |
| **Tests** | `tests/test_coordination.py`, `test_cli_coord_score.py`. **Nothing references skjoule** |

### B.4 skharness (`skcapstone-repos/skharness/src/skharness/autocode`)

| What | Where |
|---|---|
| **Assess pass** | `phase0_assess(...)` `orchestrator.py:403-510`; `harness.assess(brief)` `:462`; concreteness gate `:468-475` |
| **→ Grader emission point** | after `:475`, guarded by `if not dry_run`, as a sibling of the `board.update_task` `:480` / `close_task_obsolete` `:485` calls |
| **Verdict type** | `types.py:132-139`, `concreteness: float \| None` `:139`, size/risk are new optional fields here |
| **Reads raw dicts** | `load_raw_tasks(tasks_dir)` `orchestrator.py:424`, **not** the Task model |
| **Settlement** | `_settle_economics` `engineering.py:117-134`; `settle(...)` `joules.py:139-217`; `BuildUsage` `:56-106`; `DEFAULT_JOULE_PER_USD = 50.0` `:28` |
| **Claim call site** | `engineering.py:161-172` under `_BOARD_LOCK`, raises `ClaimRaced` |
| **Tests** | `tests/`, pytest. `test_joules.py`, `test_autopilot_orchestrator.py` (`phase0_assess` at `:67/:85/:111`) |
