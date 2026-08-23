# ai-runner go-live checklist

The fleet suggestion engine's runner (`skcapstone.agent_run.run_ai_runner_job`,
scheduled by `jobs.yaml` on noroc2027) is **plan-only** today: with
`SKAI_RUNNER_LIVE` unset it records a plan and moves the card to review, never
dispatching a live agent. This runbook is the gate for turning real dispatch on.

## Safety model (what is already enforced)

- **`SKAI_RUNNER_LIVE`** (default unset) is the master switch. Unset ⇒ no live
  dispatch at all.
- **Execute is fail-closed (R1).** Even with `SKAI_RUNNER_LIVE=1`, an `execute`
  run is NEVER sent to the raw `claude -p` dispatcher. It requires an explicitly
  wired sandboxed/graded executor via `agent_run.set_execute_dispatcher(fn)`.
  With none wired, execute records a plan and moves to review (`gated=True`,
  reason "execute requires the sandboxed executor (R1)"). Propose/dry-run use
  the passed-in dispatcher (no real side effects).
- **Defense in depth.** `claude_dispatcher` itself refuses `mode == "execute"`,
  so even mis-wiring it as the execute dispatcher cannot run an execute run raw.
- **Kind gate.** `gate()` blocks execute on `change` kinds (CAB vote required)
  and clamps GTD execute to draft-only (never auto-send).
- **Queue authz.** Queuing an execute run needs the `agentrun.execute`
  capability (verified enrollment) once `SKAI_AUTHZ=pdp|both`
  (skdashboard.queue_authz); the assistant surface cannot queue execute at all.

## Before setting SKAI_RUNNER_LIVE=1

1. **Wire the sandboxed executor.** The bridge is built
   (`skharness.autocode.agentrun_bridge`, card 0f0a291b): set `SKAI_EXECUTE_BRIDGE=1`
   in the runner env so `run_ai_runner_job` lazily wires it via
   `set_execute_dispatcher`. It runs one sandboxed round → `ratify` twin-gate
   grade → commit/push/**draft** PR, structurally incapable of merging. Every
   missing prerequisite fail-closes (execute stays plan-only). Prerequisites the
   bridge checks, each of which must be satisfied for a repo to be eligible:
   - `~/.skcapstone/config/autopilot.yaml` (or `SKOS_AUTOPILOT_CONFIG`) exists
     with a non-empty `repo_map`, and a resolvable harness (`claude-code`).
   - `live_execution: true` in that file. NOTE: this also arms the manual
     `skos autopilot run --canary` path; keep `enabled: false` + the kill switch
     so the scheduled autopilot engine stays off.
   - The card being executed carries exactly one `repo:<name>` label (0 or >1 is
     refused); no repo is inferred from instruction text.
   - The target repo is NOT automerge-enabled, and has an explicit
     `min_quality: direct` (an UNSET `min_quality` coerces to `gated`, which the
     P1 bridge refuses because it does not provide the crown-jewel gated engine).
   The bridge opens the PR as a **draft**; a human merges after review.
2. **Verify propose/dry-run first.** Enable live dispatch with execute still
   fail-closed; confirm propose/dry-run runs behave (plans/scratch diffs land in
   review) before trusting execute.
3. **Flip authz.** Set `SKAI_AUTHZ=both` (token + PDP) so execute requires
   `agentrun.execute` (verified). Confirm an unverified caller is denied.
4. **Confirm no off-loopback exposure** of the queue routes until PDP is on
   (R2). The dashboard queue gate is loopback-open only while neither
   `SKAI_AUTHZ` nor `SKAI_QUEUE_TOKEN` is set.
5. **Canary one card.** Queue a single low-risk execute run, watch it produce a
   draft PR, review it, then widen.

## Canary result (2026-08-13)

Proven live once, controlled one-shot on skos via a direct `execute_dispatch`:
- First run **fail-closed** cleanly on the missing `sandbox-claude:1` image (no
  side effect). Built it with `skharness/docker/sandbox/build.sh claude`.
- Re-run produced a real **draft** PR `smilinTux/skos#19` (branch
  `autopilot/airun-<card>`): `isDraft: true`, exactly the one intended file
  changed, independent grade 3/5 (advisory), isolated worktree pruned after.

## Turning it on (noroc2027, the scheduler host)

The `ai-runner` job (`jobs.yaml`, every 60s) runs a `run_ai_runner_job` callback
INSIDE the `skcapstone.service` user daemon on noroc2027, so the switch is that
daemon's environment. Use an isolated live config (a dedicated repo set with
`min_quality: direct`), never Chef's shared `autopilot.yaml`.

```bash
# on noroc2027 (or over ssh). A drop-in, not a unit edit:
mkdir -p ~/.config/systemd/user/skcapstone.service.d
cat > ~/.config/systemd/user/skcapstone.service.d/ai-runner-live.conf <<'EOF'
[Service]
Environment=SKAI_RUNNER_LIVE=1
Environment=SKAI_EXECUTE_BRIDGE=1
Environment=SKOS_AUTOPILOT_CONFIG=%h/.skcapstone/config/autopilot-live.yaml
EOF
systemctl --user daemon-reload
systemctl --user restart skcapstone.service
```

Prereqs on noroc2027 (verified 2026-08-13): `sandbox-claude:1` image built, gh
authed, `~/.claude/.credentials.json` present (mounted read-only into the
sandbox), the target repos checked out at their `repo_map` paths. Host `claude`
is NOT on PATH there, so propose/dry-run runs no-op gracefully ("claude CLI not
found"); only sandboxed **execute** does real work.

## Expected workload once live

- The runner is **demand-driven**: each 60s tick, `run_once` claims up to 5
  QUEUED runs (`list_queued`) and processes them sequentially. With nothing
  queued it does nothing. It never invents work.
- Only an `execute` run whose card carries one `repo:<eligible>` label runs the
  sandboxed bridge; everything else fail-closes to a plan in review.
- Each eligible execute run: one confined `sandbox-claude:1` container in an
  isolated worktree, `ratify` grade, then a **draft** PR (never a merge). Bounded
  by the live config `caps` (`max_concurrent`, `max_usd_per_day`).
- Outcome per run: the card moves to `review` with `meta.agent_run` carrying the
  state + a `links.pr` draft url; a human reviews/merges/closes.

## Monitoring

```bash
# on noroc2027
journalctl --user -u skcapstone.service -f | grep -iE "ai-runner|agent_run|bridge"
# cards that ran (state + PR link), from anywhere (synced CardStore):
skcapstone coord kanban            # runs land in the review column
# open draft PRs the bridge created:
gh pr list --search "head:autopilot/airun-" --draft
docker ps | grep sandbox-claude    # a run in flight shows a live container
```

## Rollback / kill switch

```bash
# instant stop: remove the drop-in (or just SKAI_RUNNER_LIVE) and restart.
rm ~/.config/systemd/user/skcapstone.service.d/ai-runner-live.conf
systemctl --user daemon-reload && systemctl --user restart skcapstone.service
```

Removing `SKAI_RUNNER_LIVE` reverts execute to fail-closed and the runner to
plan-only immediately; in-flight runs finish their current container. No queued
run is lost (it just records a plan instead of dispatching).
