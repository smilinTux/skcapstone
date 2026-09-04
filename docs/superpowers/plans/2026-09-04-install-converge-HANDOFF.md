# Install-and-converge: overnight handoff, 2026-09-04 06:11

Written by Lumina (Opus 5) at the end of the overnight run. Read this before
touching any of the branches below.

## One-line state

Phase 0 is built and proven end to end. Phases 1 to 5 are partly done and
mostly queued. **Six of the thirty cards did not exist when the plan was
written**; they were found by doing the work, and several are more important
than the cards that found them.

- Spec: `docs/superpowers/specs/2026-09-04-skworld-install-converge-design.md`
- Plan: `docs/superpowers/plans/2026-09-04-skworld-install-converge.md`

## THE THREE THINGS THAT NEED CHEF

### 1. `.100` probably needs a physical power cycle

It answers ping, TCP 22 and 11434 accept connections, but SSH times out during
banner exchange and every service on it is wedged. Tailscale reports
`skworld-100` offline, last seen 2 days ago. Nothing can be done for it
remotely.

### 2. `sk-default`, the estate's mandated default model, is DOWN

`CLAUDE.md` says every triage/parse/summarize step uses `sk-default`.
`~/.skcapstone/models/registry.yaml` maps `roles.sk-default -> ornith ->
192.168.0.100:8082`, which is the dead host above. A real completion returns
HTTP 000 after 40s. Card `0e0a0300`, priority critical.

**Decision needed:** revive `.100`, or repoint `sk-default` at
chiap01/chiap08 qwen38. Lumina did not move the role without authorization.

### 3. A sovereignty leak is latent behind it

`registry.yaml` lines 22-25 record, in Chef's own words, that during a previous
~4h `.100` outage `sk-default` kept returning 200 served by NVIDIA cloud
(`openai/gpt-oss-20b`, `data_retention: trains`, trust zone 2). Traffic
configured for sovereign metered hardware left the box and nothing in the
response said so. Today it fails closed instead, which is safer, but the path
still exists. Covered by a criterion on card `0e0a0300`.

## Branches

Phase 0 is a LINEAR STACK. The tip carries everything:

```
feat/fleet-materialize-render   7ad99db  P0.1 render unit text (6 tests)
  feat/fleet-materialize-apply  01858a1  P0.2 apply idempotently (6 tests)
    feat/fleet-install-materialize 0897280 P0.3 wire into fleet install (12 tests)
      fix/timer-never-fires     6a49845  timer defect fix (8 tests)   <-- TIP
```

**`fix/timer-never-fires` is the integration branch for all of Phase 0.**
Worktree: `~/skworld-worktrees/p0a4-timerfix-skcapstone`.

Independent:

```
feat/atlas-three-registry-reconcile  cdc42bf  P5.1 reconciler (16 tests)
feat/atlas-eyes-delta                (agent running)  P5.2
feat/install-converge-2026-09-04     a79bd6e  spec + plan + this file
```

Other repos:
- skgateway `feat/repair-backend-endpoints`, PR #114, 1784/1784 tests
- skgateway `feat/openrouter-enable`, commit 91886ad
- skos `feat/skos-dataroot-noroc2027`, PR #34 (doc fixes for three phantom commands)
- clawd `docs/parallel-dev-protocol`, ee1a3b5 (needs merging, see below)

## Discovered work, in order of importance

| Card | Finding |
|---|---|
| `0e0a0300` | **CRITICAL.** `sk-default` routes to a dead host. See above |
| `0e0a0400` | **DONE.** Interval timers never fired. `OnUnitActiveSec` alone gives `NextElapseUSecMonotonic=infinity`. Would have installed skfleet-rotate healthy-looking and dispatching nothing |
| `0e0a0500` | Phase 2 is a different shape. All 14 live Service objects declare `unit:` and NONE declares `exec`. They reference externally installed units rather than declaring them, including `claude-code-api.service` and `skcapstone@lumina.service`. Converging means overwriting working production units, one at a time |
| `0e010700` | The live gateway does NOT read the repo config. `SKGATEWAY_CONFIG` points at `~/.skcapstone/gateway/skgateway.yaml`: Syncthing-replicated, untracked, `versioning=NONE`, no rollback. The repo config affects nothing |
| `0e0a0200` | 18 fleet placements, 0 job objects. The scheduler reports placements for work with no definition. Agent investigating |
| `0e0a0100` | The test suite is not a usable merge gate. See below |

## The test suite cannot currently gate a merge

Measured across three complete 7301-test runs:

- **1 deterministic failure**: `test_skfleet_pi_tool_allowlist::test_pi_denies_a_direct_mcp_tool_and_measures_schema_bytes` (environment-dependent, tool list resolves to `bash/edit/read/write`).
- **3 flaky**: `test_skfleet_dispatch_integrity::test_five_host_candidate_inventory_counts_unique_ids`, `test_notification_e2e::test_interaction_recorded_as_memory`, `test_scheduler_integration::test_dreaming_reflection_job_fires_end_to_end`. Each passes in isolation. Two runs of the SAME branch gave 4 failures then 2.

Consequence: a red run is not evidence of a regression and a green run is not
evidence of correctness. Fix by isolating the leaked shared state, never by
reordering or adding retries.

**Two traps that cost real time tonight:**
1. `pytest -x` stops at the first failure and reports about 1080 of 7301 tests,
   which reads like a pass. It fooled the author of this document.
2. Always confirm a suspected regression by running that test in ISOLATION on
   the same branch before blaming a change.

## Things deliberately NOT done

- **The fleet was never unfrozen.** `~/.skcapstone/fleet/objects/_freeze.json`
  is `frozen: true`, PGP-signed by Chef on 2026-08-20, reason "repeated
  skgateway restart actuation with no firing conditions". `sknoded` runs a 60s
  converge loop against it, so unfreezing re-arms exactly what the freeze
  stops. P0.3 proved the chain against a throwaway `SKFLEET_ROOT` instead and
  said so up front. Unfreezing is Chef's decision alone.
- **`sk-default` was not repointed.** Needs authorization.
- **skharness untouched**, per instruction. Note `skos.autopilot` delegates
  into `skharness.autocode`, so they are coupled in code; decoupling is
  separate work.
- **PR45 on sk-standards was not merged.** ADR-0006 was ratified for the
  `chef` estate by the operator; ratifying it for chi is a chi decision.

## Corrections the author owes the record

1. **A false blocker cost Chef effort.** The OpenRouter key was already on the
   box in `~/.config/skgateway/secrets.env` since Aug 16, which is what the
   service reads via `EnvironmentFile`. Lumina checked `env`, `environment.d`
   and skvault, declared it missing, and raised it as a blocker. OpenRouter was
   live in production the whole time.
2. **Branching happened in the shared checkout** before the repo CLAUDE.md
   loaded. Corrected: the branch was moved to a worktree and the shared
   checkout returned to `main`. This is why the parallel-work protocol below
   was rewritten.
3. **A full suite was reported as "1079 passed"** when `pytest -x` had stopped
   it at 15% of 7301 tests.
4. **Dependencies were authored as preferred ordering**, and the board enforces
   them as hard gates. Four cards whose work was already done were falsely
   blocked. Five non-technical gates were removed with a recorded reason. Rule
   going forward: dependencies encode technical blocking only; ordering
   preference goes in priority.

## Process changes made tonight

- `~/clawd/CLAUDE.md` parallel-work protocol rewritten. The core addition:
  **a worktree isolates files, it does not isolate the machine.** Two agents in
  two clean worktrees still collide when both restart skgateway or both mutate
  the fleet store. Adds an explicit claim list for shared runtime state, a
  single worktree root with the measured evidence (29 worktrees across 6 roots,
  7 of them in `/tmp`), `<card-id>-<purpose>-<repo>` naming, and a cleanup step.
  **Committed on branch `docs/parallel-dev-protocol` and NOT yet merged.**
- **`~/clawd/CLAUDE.md` exists only on branch `fix/retire-model-name-allowlist`,
  not on `main`.** A fresh clone gets no agent instructions at all. Worth
  fixing.

## Where to resume

1. Answer the three Chef decisions above.
2. `0e010300` (cold-start admission) unblocks ten cards. An agent is on it. Its
   premise was rewritten: `observed:false` is NOT a defect, it is the correct
   fresh state that flips true after traffic. The real problem is that no lane
   can be admitted until traffic flows, so the fleet cannot bootstrap itself.
3. Then Phase 2, but read `0e0a0500` first: converging the 14 reference-style
   Service objects is riskier than the plan assumed.
