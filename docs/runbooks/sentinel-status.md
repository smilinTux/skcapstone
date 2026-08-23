# Runbook: Sentinel status surface

Card `70c4e4a2`. This runbook covers what the **Sentinel** is, how to read its
status, how to tell healthy from unhealthy from absent, and how to remediate.

## What the Sentinel is

"Sentinel" is the security-first role in SKCapstone's agent-team system. It is
the **team lead (manager)** of the `infrastructure-guardian` blueprint
(`src/skcapstone/blueprints/builtins/infrastructure-guardian.yaml`): it triages
security findings and delegates hardening work to the rest of the team. The same
role appears as `security-sentinel` in the ops-team compose template
(`docker/compose-templates/ops-team.yml`) and carries the soul blueprint
`souls/sentinel.yaml`.

Concretely, in a running system the Sentinel is a **deployed agent instance**
(a `DeployedAgent` in `team_engine.py`) whose `agent_spec_key` is `sentinel`.
It is created when you deploy a team that includes the role:

```bash
skcapstone agents deploy infrastructure-guardian
```

Because the Sentinel guards a team's security posture, knowing whether it is
running is itself a security signal: an **absent** Sentinel means the posture it
watches is unmonitored.

## The status surface

The Sentinel's health is surfaced through the existing **Trustee** machinery
(`TrusteeOps` in `src/skcapstone/trustee_ops.py`), the same path that powers
`skcapstone agents health` and the autonomous `skcapstone agents monitor`. No
parallel monitor is introduced.

There are two views:

1. **Whole-team health** (existing): every agent in one deployment.

   ```bash
   skcapstone agents health <deployment-id>
   ```

2. **Focused role status** (this card): the Sentinel by name or role, resolved
   across every deployment, with explicit absent detection.

   ```bash
   # Search all deployments for the sentinel role
   skcapstone agents health --agent sentinel

   # Narrow to one deployment
   skcapstone agents health <deployment-id> --agent sentinel
   ```

The `--agent` value matches either an agent's full instance name (e.g.
`infrastructure-guardian-sentinel`) or its role/spec key (`sentinel`),
case-insensitively.

### Programmatic access

- Python: `TrusteeOps(engine, home).agent_health("sentinel")` returns a dict with
  `present`, `healthy`, `status`, `deployment_id`, `name`, `spec_key`, `host`,
  `last_heartbeat`, and `error`.
- MCP: the `trustee_health` tool now accepts an optional `agent_name`. When set,
  it returns the focused single-agent result (including the absent case) instead
  of the whole-deployment report.

Under the hood the focused lookup runs a **live** provider health check (via the
configured provider's `health_check`), so the reported status reflects current
process state rather than only the last value written to disk. Without a provider
it falls back to the last-known status.

## Reading the result: healthy vs unhealthy vs absent

| State | `present` | `healthy` | `status` | Meaning |
|-------|-----------|-----------|----------|---------|
| Healthy | `True` | `True` | `running` | Sentinel is deployed and its live health check passed. |
| Unhealthy | `True` | `False` | `degraded` / `failed` / `stopped` / `pending` | Sentinel is deployed but not running cleanly. |
| Absent | `False` | `False` | `absent` | No agent matching `sentinel` is deployed in any team. The role is not being run at all. |

The CLI renders a green panel for healthy, a yellow panel for unhealthy (with any
`error` string), and a red panel for absent.

Healthy signals to confirm:

- `status` is `running`.
- `last_heartbeat` is recent (the live check refreshes it on a passing run).
- `error` is empty.

Unhealthy / absent signals:

- `status` is `degraded`, `failed`, `stopped`, or `pending`.
- `status` is `absent` (the role is not deployed anywhere).
- A non-empty `error` field.

## Remediation

### Sentinel is absent (not deployed)

The security lead is not running. Deploy the team that provides it, then
re-check:

```bash
skcapstone agents deploy infrastructure-guardian
skcapstone agents health --agent sentinel
```

If you expected it to be deployed already, list active teams to confirm what is
actually running:

```bash
skcapstone agents status
```

### Sentinel is unhealthy (degraded / failed / stopped)

1. Confirm the live status and read the error:

   ```bash
   skcapstone agents health --agent sentinel
   ```

2. Inspect recent activity for the instance:

   ```bash
   skcapstone agents logs <deployment-id> --agent <sentinel-instance-name>
   ```

3. Restart the Sentinel (provider stop then start):

   ```bash
   skcapstone agents restart <deployment-id> --agent <sentinel-instance-name>
   ```

4. If it keeps degrading (context rot, wedged process), rotate it. This
   snapshots its memory first, then redeploys fresh:

   ```bash
   skcapstone agents rotate <deployment-id> --agent <sentinel-instance-name>
   ```

5. Re-check until `status` is `running`:

   ```bash
   skcapstone agents health --agent sentinel
   ```

### Hands-off remediation

The autonomous monitor already restarts on stale heartbeats, rotates after
repeated failures, and escalates critical degradation to Chef. Run a single pass
or leave it watching:

```bash
skcapstone agents monitor --once
skcapstone agents monitor
```

## Related code

- `src/skcapstone/trustee_ops.py` (`TrusteeOps.agent_health`, `_live_agent_health`,
  `health_report`)
- `src/skcapstone/cli/agents_trustee.py` (`skcapstone agents health --agent`)
- `src/skcapstone/mcp_tools/trustee_tools.py` (`trustee_health` with `agent_name`)
- `src/skcapstone/team_engine.py` (`DeployedAgent`, `AgentStatus`)
- `src/skcapstone/blueprints/builtins/infrastructure-guardian.yaml` (the `sentinel` role)
