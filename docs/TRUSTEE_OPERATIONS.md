# Trustee Operations Manual

Operating manual for AI trustees: what a trustee is, the deployment lifecycle,
how to drive every operation via the CLI and MCP tools, health and alerting,
context rotation, scaling, and a failure/recovery runbook.

All commands and signatures in this document are drawn from the real code.
File references are given as `path:line` so you can verify any claim.

## What an AI trustee is

A trustee is an operator (AI or human) who manages deployed agent teams on
behalf of the sovereign. Trustees run the operations surface in
`src/skcapstone/trustee_ops.py` and the autonomous watcher in
`src/skcapstone/trustee_monitor.py`.

The module header names the intended trustees explicitly: AI trustees Lumina
and Opus, and human trustee Chef, all "operating under the Trustee Oath"
(`src/skcapstone/trustee_ops.py:8-11`):

> "I escalate when uncertain — never guess with sovereignty."

The oath is not decorative. It is the design contract for the monitor's
escalation path (`src/skcapstone/trustee_monitor.py:11-12`): when a trustee
cannot safely remediate, it escalates to Chef rather than guessing. Every
mutation a trustee makes is written to an append-only audit trail so the work
is transparent and accountable (`src/skcapstone/trustee_ops.py:4-6`).

### Core objects

- `TrusteeOps`: high-level operations over one deployed team (restart, scale,
  rotate, health report, logs). Constructed from a `TeamEngine` plus an agent
  `home` directory (`src/skcapstone/trustee_ops.py:38-52`).
- `TrusteeMonitor`: autonomous loop that watches deployments and takes
  corrective action (`src/skcapstone/trustee_monitor.py:85-106`).
- `MonitorConfig`: the thresholds and on/off toggles the monitor obeys
  (`src/skcapstone/trustee_monitor.py:29-49`).
- A `TeamDeployment` holds a map of `DeployedAgent`, each carrying a
  `status`, `host`, `last_heartbeat`, and `error`. Agent status is one of
  `pending`, `running`, `degraded`, `stopped`, `failed`
  (`src/skcapstone/team_engine.py:46-55`).

### Where trustee state lives

- Audit trail: `<home>/coordination/audit.log`, one JSON object per line
  (`src/skcapstone/_trustee_helpers.py:24-53`). Default home is
  `~/.skcapstone` (`src/skcapstone/trustee_ops.py:52`).
- Rotation snapshots: `<home>/snapshots/<agent>-<UTC-timestamp>/`
  (`src/skcapstone/_trustee_helpers.py:95-107`).
- Per-agent logs: `<home>/agents/local/<agent>/agent.log`
  (`src/skcapstone/trustee_ops.py:412-418`).
- Inter-agent message archives: `<home>/comms/<deployment_id>/<agent>/archive/`
  (`src/skcapstone/cli/agents_trustee.py:311-357`).

## Lifecycle overview

```
deploy → status/health → logs/messages → (restart | rotate | scale) → destroy
                              ↑                                          
                      monitor watches continuously and auto-remediates
```

A trustee does not create deployments through the trustee surface. Teams are
deployed from blueprints, and the trustee operations then manage the running
fleet. The two CLI groups are:

- `skcapstone agents deploy | status | destroy` for lifecycle
  (`src/skcapstone/cli/agents.py`).
- `skcapstone agents restart | scale | rotate | health | logs | messages |
  monitor` for trustee operations
  (`src/skcapstone/cli/agents_trustee.py`).

The MCP surface exposes the operations (not deploy/destroy) as seven tools:
`trustee_health`, `trustee_restart`, `trustee_scale`, `trustee_rotate`,
`trustee_monitor`, `trustee_logs`, `trustee_deployments`
(`src/skcapstone/mcp_tools/trustee_tools.py:9-157`).

## Deploy (lifecycle entry)

Deploy a team from a blueprint slug
(`src/skcapstone/cli/agents.py:177-186`):

```bash
skcapstone agents deploy <slug>
skcapstone agents deploy dev-squadron --provider proxmox
skcapstone agents deploy research-pod --name "my-research-team"
```

- `--provider` choices: `local`, `proxmox`, `hetzner`, `aws`, `gcp`, `docker`
  (`src/skcapstone/cli/agents.py:181-185`). Omit to use the blueprint default.
- `--name` sets a custom deployment name; `--home` overrides the agent home.
- Deploy prints the resulting `deployment_id`. Capture it: every trustee
  operation below is keyed on that id
  (`src/skcapstone/cli/agents.py:263`, `301-306`).

List running deployments and per-agent status
(`src/skcapstone/cli/agents.py:309-311`):

```bash
skcapstone agents status
```

## Health monitoring

### One-shot health report

CLI (`src/skcapstone/cli/agents_trustee.py:175-178`):

```bash
skcapstone agents health <deployment_id>
```

MCP (`src/skcapstone/mcp_tools/trustee_tools.py:10-26`):

```
trustee_health(deployment_id)
```

`health_report()` calls `provider.health_check` for each agent when a provider
is available, updates the live status, refreshes `last_heartbeat` for running
agents, and persists the deployment. With no provider it returns the cached
on-disk status (`src/skcapstone/trustee_ops.py:315-369`). Each row reports
`name`, `status`, `host`, `last_heartbeat`, `error`, and a boolean `healthy`
(healthy means `status == running`) (`src/skcapstone/trustee_ops.py:353-360`).

The MCP handler adds a summary block with `total`, `healthy`, and `degraded`
counts (`src/skcapstone/mcp_tools/trustee_tools.py:186-195`).

### Fleet overview

MCP only (`src/skcapstone/mcp_tools/trustee_tools.py:149-156`):

```
trustee_deployments()
```

Returns every deployment with `blueprint_slug`, `team_name`, `provider`,
`status`, `agent_count`, and a per-agent status/host/heartbeat map
(`src/skcapstone/mcp_tools/trustee_tools.py:309-334`). The CLI equivalent for
humans is `skcapstone agents status`.

### Logs

CLI (`src/skcapstone/cli/agents_trustee.py:241-245`):

```bash
skcapstone agents logs <deployment_id>
skcapstone agents logs <deployment_id> --agent <agent_name> --tail 20
```

MCP (`src/skcapstone/mcp_tools/trustee_tools.py:124-147`):

```
trustee_logs(deployment_id, agent_name=None, tail=50)
```

`get_logs()` reads `<home>/agents/local/<agent>/agent.log` and returns the last
`tail` lines. When no dedicated log file exists it falls back to audit-log
entries filtered by that agent and deployment
(`src/skcapstone/trustee_ops.py:375-425`, fallback at
`_trustee_helpers.py:115-149`).

### Inter-agent messages (CLI only)

Audit what agents are saying to each other, reading archived envelopes from the
team comms channel (`src/skcapstone/cli/agents_trustee.py:286-297`):

```bash
skcapstone agents messages <deployment_id>
skcapstone agents messages <deployment_id> --agent <agent_name>
skcapstone agents messages <deployment_id> --limit 50
```

## Autonomous monitoring and alerting

The monitor is the heart of trustee autonomy. Its escalation ladder is
strictly ordered: **restart → rotate → escalate**
(`src/skcapstone/trustee_monitor.py:88-90`, `292`).

### Run the monitor

CLI, continuous or single pass
(`src/skcapstone/cli/agents_trustee.py:462-511`):

```bash
skcapstone agents monitor                                   # all deployments, loop
skcapstone agents monitor --interval 15 --deployment <id>   # one deployment, 15s
skcapstone agents monitor --once                            # single pass, then exit
skcapstone agents monitor --no-escalate --heartbeat-timeout 60
```

Flags (`src/skcapstone/cli/agents_trustee.py:463-483`):

- `--interval`, `-i`: seconds between checks (default 30).
- `--deployment`, `-d`: monitor only this deployment (default: all).
- `--heartbeat-timeout`: seconds since last heartbeat before auto-restart
  (default 120).
- `--max-restarts`: consecutive restart failures before auto-rotate
  (default 3).
- `--no-restart`, `--no-rotate`, `--no-escalate`: disable each automation.
- `--once`: run one pass and print the report.

MCP single pass (`src/skcapstone/mcp_tools/trustee_tools.py:94-123`):

```
trustee_monitor(deployment_id=None, heartbeat_timeout=120,
                auto_restart=True, auto_rotate=True)
```

The MCP tool always runs exactly one pass (`check_deployment` if
`deployment_id` given, else `check_all`) and returns counts:
`deployments_checked`, `agents_healthy`, `agents_degraded`,
`restarts_triggered`, `rotations_triggered`, `escalations_sent`
(`src/skcapstone/mcp_tools/trustee_tools.py:258-287`). For a persistent
watcher, use the CLI loop.

### What the monitor decides

Per agent, each pass (`src/skcapstone/trustee_monitor.py:273-315`):

1. A running agent with a fresh heartbeat is counted healthy; any prior
   incident counters are cleared on recovery
   (`src/skcapstone/trustee_monitor.py:280-286`).
2. A stale heartbeat is a heartbeat older than `heartbeat_timeout` seconds; a
   missing heartbeat on a `running` agent is also treated as stale
   (`src/skcapstone/trustee_monitor.py:124-143`).
3. If restart attempts have reached `max_restart_attempts` (default 3), the
   monitor rotates instead of restarting again
   (`src/skcapstone/trustee_monitor.py:293-297`).
4. Otherwise, if the agent is stale, `failed`, or `degraded`, it is restarted
   (`src/skcapstone/trustee_monitor.py:299-301`).

### Escalation

After the per-agent sweep, if the failed fraction reaches `critical_threshold`
(default 0.5, i.e. half or more agents down) the monitor escalates
(`src/skcapstone/trustee_monitor.py:303-313`). Escalation sends a
`critical`-urgency message to `chef` via the SKChat transport
(`_send_message_impl`), prefixed `[TRUSTEE ESCALATION]`
(`src/skcapstone/trustee_monitor.py:214-246`). Escalations honor
`escalation_cooldown` (default 300s) so a flapping deployment cannot spam Chef
(`src/skcapstone/trustee_monitor.py:227-231`). If no transport is available the
message is logged instead of dropped
(`src/skcapstone/trustee_monitor.py:242-246`).

Threshold defaults live in `MonitorConfig`
(`src/skcapstone/trustee_monitor.py:43-49`):

| Setting | Default | Meaning |
|---------|---------|---------|
| `heartbeat_timeout` | 120.0s | Silence before an agent is stale |
| `max_restart_attempts` | 3 | Failed restarts before rotate |
| `critical_threshold` | 0.5 | Failed fraction that triggers escalation |
| `escalation_cooldown` | 300.0s | Minimum gap between escalations |
| `auto_restart` / `auto_rotate` / `auto_escalate` | True | Automation toggles |

## Restart

CLI (`src/skcapstone/cli/agents_trustee.py:43-54`):

```bash
skcapstone agents restart <deployment_id>                 # whole team
skcapstone agents restart <deployment_id> --agent <name>  # one agent
```

MCP (`src/skcapstone/mcp_tools/trustee_tools.py:27-47`):

```
trustee_restart(deployment_id, agent_name=None)
```

`restart_agent()` calls `provider.stop` then `provider.start` for each target,
sets the agent back to `running` with a fresh heartbeat, clears its error, and
records the outcome per agent as `"restarted"` or `"error: ..."`. Any failure
marks that agent `failed` and stores the error. Results are audited under
action `restart_agent` (`src/skcapstone/trustee_ops.py:87-144`).

## Rotate (context refresh)

Use rotation when an agent shows context degradation or context fill, not just
a crash. Rotation snapshots the agent's memory before destroying it so nothing
is lost, then redeploys fresh (`src/skcapstone/trustee_ops.py:241-249`).

CLI (`src/skcapstone/cli/agents_trustee.py:131-135`):

```bash
skcapstone agents rotate <deployment_id> --agent <agent_name>
```

MCP (`src/skcapstone/mcp_tools/trustee_tools.py:73-93`):

```
trustee_rotate(deployment_id, agent_name)
```

Sequence (`src/skcapstone/trustee_ops.py:261-309`):

1. Snapshot `<home>/agents/local/<agent>/` to
   `<home>/snapshots/<agent>-<UTC>/` (the snapshot dir is created even if the
   source is absent) (`src/skcapstone/_trustee_helpers.py:83-107`).
2. `provider.destroy` the agent.
3. Re-provision, configure, and start it fresh via the provider.
4. Return `snapshot_path`, `destroyed`, and `redeployed`; audited under action
   `rotate_agent`.

The monitor auto-rotates an agent at most once per incident, then resets its
restart counter (`src/skcapstone/trustee_monitor.py:182-212`).

## Scale

CLI (`src/skcapstone/cli/agents_trustee.py:83-87`):

```bash
skcapstone agents scale <deployment_id> --agent <spec_key> --count 3
skcapstone agents scale <deployment_id> --agent <spec_key> --count 1
```

MCP (`src/skcapstone/mcp_tools/trustee_tools.py:48-72`):

```
trustee_scale(deployment_id, agent_spec_key, count)
```

`scale_agent()` sets the desired total instance count for one agent role
(`agent_spec_key`). `count` must be `>= 1` or it raises `ValueError`. Scaling
up provisions new instances named
`<blueprint_slug>-<spec_key>-<n>`; scaling down stops and removes the
highest-numbered excess instances. Returns `added`, `removed`, and
`current_count`; audited under action `scale_agent`
(`src/skcapstone/trustee_ops.py:150-235`).

Note: `agent_spec_key` is the role identifier, not an individual agent name.
Restart, rotate, and logs take an `agent_name`; scale takes an
`agent_spec_key`. Do not confuse the two.

## Key rotation for trustees

Trustee identity and signing keys are not rotated through the `trustee_*`
tools. Key material is managed by the KMS surface. Rotate keys with the KMS
MCP tools (`kms_list_keys`, `kms_rotate`, `kms_status`) or the CapAuth identity
flow described in the repo `CLAUDE.md`. Run `skcapstone doctor` after any
identity or key change to re-validate the unified identity layer.
`trustee_rotate` rotates an agent's runtime context (snapshot + redeploy); it
does not touch cryptographic keys. Keep the two operations distinct.

## Audit and transparency

Every mutating operation appends a JSON line to `<home>/coordination/audit.log`
with `ts`, `action`, `deployment_id`, and operation-specific detail fields
(`src/skcapstone/_trustee_helpers.py:28-53`). Audited actions:
`restart_agent`, `scale_agent`, `rotate_agent`, `health_report`, `get_logs`
(`src/skcapstone/trustee_ops.py:143`, `230-234`, `308`, `364-368`, `424`).

Because the audit log is append-only and machine-readable, it is both the
transparency record and the log fallback: `trustee_logs` reconstructs an
agent's activity from it when no `agent.log` exists
(`src/skcapstone/trustee_ops.py:419-422`).

To review the trail directly:

```bash
tail -n 50 ~/.skcapstone/coordination/audit.log
```

## Failure and recovery runbook

Follow the same ladder the monitor follows: restart → rotate → escalate. Do not
skip ahead. This is the oath in practice.

1. Confirm the symptom. Run `skcapstone agents health <deployment_id>` (or
   `trustee_health`). Note which agents are not `healthy` and read their
   `error` fields.
2. Read the evidence. `skcapstone agents logs <deployment_id> --agent <name>`
   (or `trustee_logs`). If there is no `agent.log`, the audit fallback shows
   the recent trustee actions on that agent.
3. Restart first. `skcapstone agents restart <deployment_id> --agent <name>`
   (or `trustee_restart`). Re-run health. A `"restarted"` result plus a
   `running` status means recovered.
4. If restarts do not hold (repeated failure, or you suspect context
   degradation / context fill), rotate:
   `skcapstone agents rotate <deployment_id> --agent <name>` (or
   `trustee_rotate`). The snapshot path in the result is your rollback
   evidence; nothing is discarded.
5. If half or more of a deployment is down, or you are uncertain how to
   proceed, escalate. The monitor escalates automatically at
   `critical_threshold`; a human trustee escalates to Chef directly. Never
   guess with sovereignty (`src/skcapstone/trustee_ops.py:9-10`).
6. Capacity problem rather than a crash? Scale the affected role:
   `skcapstone agents scale <deployment_id> --agent <spec_key> --count <n>`.
7. Unrecoverable deployment. Tear it down with
   `skcapstone agents destroy <deployment_id>` and redeploy from the blueprint.

To let the monitor run the ladder for you, leave a watcher running:

```bash
skcapstone agents monitor --interval 30
```

## Operator checklist

Session start:

- [ ] `skcapstone agents status`: know the fleet before you touch it.
- [ ] `trustee_deployments` (MCP) for a machine-readable overview.
- [ ] Skim recent audit entries: `tail ~/.skcapstone/coordination/audit.log`.

Before acting:

- [ ] Run `trustee_health` / `skcapstone agents health <id>` and read every
      `error` field.
- [ ] Pull logs for any degraded agent before remediating.

While remediating:

- [ ] Restart before rotate; rotate before escalate. Do not skip the ladder.
- [ ] Use `agent_name` for restart/rotate/logs; use `agent_spec_key` for scale.
- [ ] After any rotate, record the returned `snapshot_path`.

Monitoring:

- [ ] Keep a monitor running (`skcapstone agents monitor`) for any live team,
      or run `trustee_monitor` passes on a schedule.
- [ ] Confirm `--no-escalate` is NOT set on the production watcher.
- [ ] Tune `--heartbeat-timeout` to the team's real cadence to avoid false
      restarts.

Keys and identity (out of band from `trustee_*`):

- [ ] Rotate trustee keys via KMS tools / CapAuth, then run
      `skcapstone doctor`.

Transparency:

- [ ] Verify your action landed in `audit.log`.
- [ ] When you escalate, confirm Chef received the `[TRUSTEE ESCALATION]`
      message (or that it was logged if no transport was available).
