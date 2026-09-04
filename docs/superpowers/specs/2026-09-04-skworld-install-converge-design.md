# SKWorld install-and-converge: skos installs, fleet declares, Atlas reconciles

**Status:** APPROVED (design), pending implementation
**Date:** 2026-09-04
**Estate:** `chef` (noroc2027 control, node-41 builder)
**Operator:** Chef (David), `chef@skworld.io`, GPG `BD7EEECA23D90A594400751CFDB582D9CB7272A6`
**Approach:** A, install-and-converge

## 1. Problem

The estate does not have a capability gap. It has an installation gap.

Every capability requested for this work already exists as code. None of it is
installed on noroc2027, the control node. Concretely, measured 2026-09-04:

| Thing | Built? | Installed on noroc2027? |
|---|---|---|
| `skfleet-rotate.py` lane dispatcher | yes, `scripts/fleet/`, live on chiap01/02/03/08 | no. No binary, no systemd unit |
| skgateway capability probing | yes, `src/discovery/capability-assessment.mjs` | built but not observing: every backend reports `observed:false` |
| skos pack installer | yes, `packs/{loader,model,planner,provisioner}` | `SK_DATA_ROOT` unset. Never initialized |
| Atlas operator estate views | yes, `skcapstone atlas eyes\|soak` | not scheduled, not surfaced |
| Seat identities (ADR-0006) | specified in card 8d5404ca | `niobe`, `tank`, `seraph` exist nowhere, on any host |

### 1.1 Root cause

Three registries independently claim to know what runs where, and none is
authoritative:

1. **systemd user units**, what is actually running.
2. **fleet placements**, what the scheduler believes should run.
3. **the skos capability registry**, what was declared.

Nothing reconciles them. The observable symptoms:

- 22 fleet placements, of which `job/d`, `job/demo`, `job/t-1`, `job/keep` and
  two others correspond to no real workload.
- `node-ollama` has been Dead for 171605 seconds (47 hours) while still
  advertising `gpu=true`.
- 4 failed units: `push-pending`, `skchat-app-web`, `skchat-health-probe`,
  `skcot-cert-renew`.
- ~20 scratch agent homes in `~/.skcapstone/agents/` (`sovereign-test`,
  `opus-swarm-varus-urls`, `sonnet-worker-1`, `sonnet-worker-2`, and similar).
- A documented fleet setup live on four chi hosts and absent on the fifth.

`sk-standards` commit 6a446e2 (2026-09-02) already states the governing
instinct: "a managed runtime must expose entry points, not ...". It is not yet
enforced anywhere.

## 2. Thesis

**Make skos the only installer. Make fleet objects the only desired-state.
Make Atlas the reconciler that reports the delta between declared, desired and
actual.**

This is not a new subsystem. It is the wiring between three subsystems that
already exist, and the code already wants it (section 3).

## 3. Architecture: the install chain

### 3.1 Why there is no "write a systemd unit" step

`skos/packs/model.py` defines exactly six step kinds, in this canonical order:

```
sql_migration -> db_roles -> content_repo -> seed -> fleet_objects -> doctor
```

There is deliberately no step that writes a systemd unit or drops a binary in
`~/.local/bin`. `fleet_objects` writes per-node CronJob and service specs into
the fleet store, and `skcapstone fleet install --apply` actuates a node against
those objects. So the chain is already the strategic move:

```
skos pack manifest (skworld.module.json, signed)
        |
        v
  fleet_objects step
        |
        v
  fleet store object  <-- the ONLY desired-state
        |
        v
  skcapstone fleet install --apply   (on the node)
        |
        v
  systemd unit on the node           <-- actual
```

Installing `skfleet-rotate` on a node therefore becomes *a pack declaring a
CronJob*, never a hand-written unit file. That is what makes the install
idempotent and reproducible for a fresh operator, which is the stated goal.

The provisioner (`packs/provisioner.py`) is already idempotent, resumable
(`partial` re-runs resume), all-or-nothing, and side-effect-isolated behind an
injected `Effects` protocol. One pack exists today as the reference:
`packs/skbrain/`, carrying `skworld.module.json` plus two fleet CronJob specs.

### 3.2 The one unverified link

Systemd actuation lives in `fleet/{install_backends,converge,actuation,services}.py`.
A grep for CronJob-to-systemd-timer *rendering* found nothing. **This is not
assumed to work.** Card P0-1 verifies it end to end before any pack is authored,
because every later phase depends on it. If the rendering does not exist, P0-2
builds it, and that is the single largest piece of net-new code in this plan.

### 3.3 Atlas as reconciler

Atlas reads all three registries and reports the delta. It writes nothing and
actuates nothing. This sits entirely inside the existing Atlas Constitution
(`docs/ATLAS_CONSTITUTION.md`), which already charters Atlas as read-only over
apps and infra, and which `docs/fleet/seat-charters.md` reinforces with "the
coordination board, which it provably does not read".

Consequence: adding the delta report grants Atlas **no new authority**. That is
what reconciles the "ATLAS remains frozen and last" clause of card 8d5404ca with
the operator's instruction to stand Atlas up as the ops seat. Atlas gets an
identity and a read-only report, in the last phase, and nothing else.

## 4. Seat model

The estate adopts **ADR-0006** (`smilinTux/sk-standards` PR45, open and
MERGEABLE as of 2026-09-03T20:33Z, not yet merged). The operator has ratified it
for the `chef` estate. Since `niobe`, `tank` and `seraph` exist on no host,
noroc2027 becomes the first implementation of ADR-0006 and supplies PR45 with
evidence.

ADR-0006 content is normative in the acceptance criteria of card 8d5404ca
(`[SKSEATS-TRANSITION-01][L]`, critical, do-not-claim) on chiap08.

| Seat | Holder | Owns | Explicitly does not own |
|---|---|---|---|
| Fleet Dispatcher | `niobe` | Fleet claims, launches, releases, reassignment, rotation, lane routing, worker health. **Only the authority transferred from Jarvis, nothing added.** | Review verdicts, merge queue, application action dispatch, app actuation |
| Release / Install | `tank` | Release and install operator. Behavioral deployment verifier. | Dispatch, merge, review verdicts |
| Verification | `seraph` | Verification only. | Merge, dispatch, actuation. All three, explicitly |
| Integrator | `link` | Triage, independent-review assignment, merge queue, evidence-bound technical arbitration. Escalates ambiguity to Chef. | Fleet claims, launches, releases, actuation |
| Overseer | `mero` | Read-only convergence and drift measurement. Typed recommendations, alerts, briefs. | Any mutation, merge, or actuation. Repairing what it measures |
| Operations | `atlas` | Apps and infra, read-only, under the Atlas Constitution. **Frozen: no new authority. Provisioned last.** | The coordination board. Any actuation added by this work |
| Decision recorder | `chef` | Signed Chef artifact or ITIL ingestion is the mechanical decision-recording authority. Seraph verifies effect. | n/a. This is the human operator, not an AI agent |

`jarvis` receives **no seat on this estate**. Per ADR-0006 it stands down to
Casey personal-agent duties, which is a chi concern, not a `chef` estate concern.

### 4.1 Typed recommendation contract (carried forward from ADR-0005)

`mero` and `link` MAY append a `skfleet.dispatch-recommendation/v1` event to a
card. The event is advice, never an instruction, and MUST carry `card_id`,
`recommendation_id`, `recommender`, `observed_at`, `observed_claim_owner`,
`observed_claim_revision`, `observed_process`, `reason`, `evidence_sha256`.

Only the dispatcher seat (`niobe` on this estate) may act on it, and only after
re-reading the current CardStore owner, claim revision and process state, and
rejecting duplicate `recommendation_id`, mismatched claim revision, or stale
process evidence.

### 4.2 Standing dev-lifecycle role agents

All receive **full operator-signed capauth identities**, not lightweight
profiles. Rationale: `veritas`'s entire mandate is "no agent reviews its own
work", and a `capauth_managed: false` profile cannot sign, so it cannot prove it
is Veritas. Separation of duties that cannot be cryptographically attributed is
a naming convention, not a control.

| Agent | Role |
|---|---|
| `veritas` | Independent reviewer. Never reviews what it implemented. Never modifies product source |
| `implementer` | Card implementation |
| `tester` | Verification of implementation against acceptance criteria |
| `cli-reviewer` | CLI-surface review |
| `dependency-worker` | Dependency bumps |
| `fleet-review-closer` | Closes out stale review cards |
| `fleet-lifecycle-reconciler` | Reconciles card lifecycle state |

Total identities to mint: 6 seats (`niobe`, `tank`, `seraph`, `link`, `mero`,
`atlas`) plus 7 role agents = **13**.

### 4.3 Identity ceremony

Per `docs/fleet/standing-up-a-seat.md`, with estate substitutions. Name must
match `^[a-z][a-z0-9-]{0,31}$` because it reaches a shell command line, a tmux
session name, a claim owner and a mailbox name.

```bash
umask 077
PF=~/capauth-pass/<name>.passphrase.env
openssl rand -base64 36 | tr -d "\n/+=" | cut -c1-48 > "$PF"; chmod 600 "$PF"

D=~/.skcapstone/agents/<name>/capauth
mkdir -p "$D"; chmod 700 "$D"
capauth --home "$D" init -n <Name> -e <name>@chef.skworld.io \
        --type ai --algorithm rsa4096 -p "$(cat $PF)"
```

Three non-negotiables carried from the chi runbook:

1. **Never omit `--home`.** A seat that inherits the default profile produces
   signatures that are really the default agent speaking.
2. **`capauth init` exits 1 while still producing a valid keypair.** Check the
   result, not the exit code.
3. **Derive the fingerprint from the key material, not from `profile.json`**, and
   assert it differs from every other seat. A verify that only ever returns True
   proves nothing, so each attestation is checked against both the true payload
   (must verify) and a tampered payload (must fail).

Operator is `chef`, fqid is `<name>@chef.skworld.io`, matching the existing
convention already visible in `artisan@chef.skworld.io` and
`herald@chef.skworld.io`.

## 5. Backends and buckets

### 5.1 Inventory

The estate's real endpoints. **No z.ai, no Kimi**: neither subscription exists
here, and both are removed from consideration rather than left as dead config.

| Backend | Endpoint | Bucket | Current defect |
|---|---|---|---|
| `anthropic` | `127.0.0.1:18782` (claude-code-api, subscription) | `claude` | none. Correct today |
| `nvidia` | `https://integrate.api.nvidia.com/v1` (NIM free tier) | `nim` | not capability-probed |
| `openrouter` | `https://openrouter.ai/api/v1` (free tier) | `openrouter` | `enabled: false`, no key provisioned |
| `chiap08-qwen38` | qwen3.8-27b-huihui-abliterated-q4_k_m | `qwen` | url is the literal string `http://TAILNET_HOST:11439/v1`, unsubstituted, therefore dead |
| `chiap01-qwen38` | `http://chiap01:18810/v1` | `qwen` | none known |
| `ornith` | `.100`, ornith-1.5-9b | `ornith` | miscofigured as backend `ollama` at `192.168.0.100:11434`, dead 47h |

Direct `api.anthropic.com` calls are not used: they 400 the subscription token
for third-party apps. Claude models route via skgateway :18780 to
claude-code-api :18782, which is already correct and must not regress.

### 5.2 Lane order

Cheapest first, matching the chi lane pattern but with this estate's backends:

```
qwen  ->  ornith  ->  nim  ->  openrouter  ->  claude (escalate)
```

`claude` is the escalation lane because it is the only paid-subscription
capacity. Heavy card titles (`production`, `release`, `migration`, `schema`,
`architecture`, `[HUMAN]`, `[XL]`) are excluded from the free local lanes by the
existing `qwen_suitable` predicate.

### 5.3 Automatic capability bucketing

Requested behaviour: NIM and OpenRouter models are evaluated for tool support
and bucketed automatically rather than listed by hand.

The mechanism already exists and is wired, not built:

- `src/discovery/capability-assessment.mjs` exposes `runCapabilityAssessment`.
- `src/discovery/probe.mjs` already rides the capability battery on the existing
  health sweep, so a battery never costs an extra request, and writes
  `measured_capabilities` onto the same lifecycle record.
- `src/discovery/classify.mjs` explicitly refuses to infer capability from a
  model name. Its stated position is that "a provider which publishes no
  capability data cannot be" trusted to have the capability. 59 NVIDIA models
  currently sit in "unknown capability".

The work is therefore: enable the battery for the `nvidia` and `openrouter`
backends, and make bucket membership a function of `measured_capabilities`
rather than a hand-maintained list. A model enters the tool-capable sub-bucket
only on measured evidence, never on its name.

### 5.4 Health observation

Every backend currently reports `observed:false` in `/health`. Per
`docs/fleet/lane-admission-health.md`, admission requires a snapshot where the
domain was observed `up` or `degraded`, was not quarantined, and reports
positive queue capacity, and "missing, malformed, oversized, stale, partial,
mismatched, or ambiguous evidence does not authorize a claim".

Therefore: **with health unobserved, this node would admit zero cards even with
the rotator installed.** Fixing observation is a prerequisite to the fleet
phase, not a nicety, and is sequenced accordingly.

## 6. Worker runtime

`pi` plus models is the default worker runtime, matching chi and per operator
preference. `~/.pi/agent/models.json` declares the skgateway provider and every
model id a lane can select, because an undeclared id boots as a custom model
with no metadata.

`skharness` stays **installed and unmodified**. It is not removed and not made
the default. Note the coupling: `skos.autopilot` delegates into
`skharness.autocode` (the assess/plan/build/grade/finalize loop, the twin gate,
the constitutional carve-out detector), so skharness cannot be cleanly separated
today. Keeping both callable is an explicit requirement; the decoupling is
carded separately and is out of scope here.

## 7. Phases

Sequencing is load-bearing. Each phase depends on the one before it: packs
cannot be authored before the actuation chain is proven (P0), the fleet cannot
admit work before health is observed (P1), and seats cannot be placed before
skos has laid the data root (P2).

| # | Phase | Cards | Model | Depends on |
|---|---|---|---|---|
| 0 | Prove the install chain: CronJob to systemd rendering, end to end | 2 | Opus | none |
| 1 | skgateway: 6 backends correct, buckets, capability battery on, health observed | 6 | Fable, 1 Opus | P0 |
| 2 | skos packs: author `skfleet`, `skgateway`, `seats`; lay `SK_DATA_ROOT` on noroc2027 and node-41 | 6 | Fable, 1 Opus | P0, P1 |
| 3 | Mint 13 identities, operator-sign as `chef`, assert fingerprints distinct | 4 | Fable | P2 |
| 4 | Drift purge: junk placements, scratch agent homes, node-ollama, 4 failed units | 3 | Haiku | P3 |
| 5 | Atlas: three-registry delta report, `atlas eyes` scheduled, dashboard :7778 | 3 | Opus | all |

24 cards.

Atlas is last, by the transition card's "frozen and last" clause. The drift
purge precedes it so Atlas's first report describes a clean estate rather than
inheriting a backlog of known junk.

## 8. Model routing

| Model | Card class | Why |
|---|---|---|
| Fable 5.1 | Config stanzas, pack manifests, identity ceremony, systemd units, doc updates | High-volume mechanical work with a precise spec. The bulk of the card count |
| Opus 5 | Actuation-chain verification (P0), pack-model decisions, Atlas reconciler contract | Architectural judgement, and P0 is the card everything else rests on |
| Haiku 4.5 | Inventory greps, drift purges | Mechanical, high-volume, low-judgement |

## 9. Verification contract

Applies to every card, per the estate's standing rules.

1. **Commit before the long verification, never after.** Commit as soon as the
   code is written and a fast check passes. The message may say "verification
   pending". Then run the full suite and amend or follow up. A background job
   never gates a commit.
2. **Read state back through a different path than the one that wrote it.** Five
   SK tools are known to exit 0 and persist nothing. A CLI printing success is
   not evidence.
3. **Never trust a "done" report.** Check `git log origin/main..HEAD` and
   `git status --porcelain` in the worker's worktree before believing anything
   landed.
4. **`coord link` verdict recording is silently lossy.** It prints success with
   an empty value; 13% of evidence events are null. Read verdicts back from
   `card_events`.
5. **Was the probe entitled to succeed?** A 401 or 400 from a deny-by-default
   service is often the control working. Read the body before calling it broken.
6. Every branch, loop, parser, or security path leaves exactly one runnable
   check behind. No frameworks, no fixtures.

Commit attribution names the agent that actually did the work. No
`Co-Authored-By` footer that cannot be evidenced.

## 10. Blockers

| # | Blocker | Owner | Blocks |
|---|---|---|---|
| B1 | **OpenRouter API key.** Not in env, not in `~/.config/environment.d/`, not in skvault. `skvault unlock` needs the Chef GPG passphrase (fingerprint `BD7EEECA...`), which is a different secret from the KeePass master. The supplied master failed against all three `.kdbx` files across 7 casing variants via pykeepass. | Chef | 1 card in P1. Everything else proceeds |
| B2 | **CronJob to systemd rendering may not exist.** Unverified. | P0-1 | All of P2 onward |

B1 degrades to a single card marked blocked-on-credential. It does not stall the
plan.

## 11. Out of scope

Named explicitly so nothing is quietly dropped or quietly added.

- The ~60 `*-skworld-io` Pages sites and `skgentis` contracts. No architectural
  signal, large token cost.
- The `skcapstone-repos` second tier: pqc, pgp, seed, vault, voice.
- Decoupling `skos.autopilot` from `skharness.autocode`. Carded separately.
- Merging PR45 on sk-standards. The operator ratified ADR-0006 for the `chef`
  estate; ratifying it for chi is a chi governance decision and this work does
  not make it.
- Any expansion of Atlas authority beyond read-only observation.
- The chi estate. Read-only reference only, via `skuser01@chiap08`.

## 12. References

- `sk-standards` ADR-0005 (five operating seats), ROSTER.md
- `sk-standards` PR45, ADR-0006 proposed (dispatcher handoff)
- card `8d5404ca` `[SKSEATS-TRANSITION-01][L]`, chiap08, critical, do-not-claim
- `docs/fleet/standing-up-a-seat.md`
- `docs/fleet/model-lane-routing.md`
- `docs/fleet/lane-admission-health.md`
- `docs/fleet/card-authoring-dispatch-gates.md`
- `docs/fleet/seat-charters.md`
- `docs/ATLAS_CONSTITUTION.md`
- `docs/LIGHTWEIGHT_AGENTS.md`
- `skos/src/skos/packs/model.py`, `provisioner.py`
- `skgateway/src/discovery/{capability-assessment,probe,classify}.mjs`
