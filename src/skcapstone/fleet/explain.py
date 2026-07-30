"""Self-describing surface (spec section 8): kinds, fields, actions.

A fresh AI operator discovers the system from this registry at runtime
instead of via hardcoding. Phase 1 registers Node; each later phase adds
its kind here as part of shipping it.
"""

from __future__ import annotations

KINDS: dict[str, dict] = {
    "node": {
        "kind": "Node",
        "description": "A machine in the fleet.",
        "spec": {
            "labels": "label map used by selectors (exact match, AND)",
            "taints": "list of {key, value, effect: NoSchedule|PreferNoSchedule}",
            "cordoned": "bool; excluded from scheduling when true",
            "capacityOverrides": "optional manual capacity caps",
            "address": "LAN + tailscale addresses, ssh target",
        },
        "status": {
            "capacity": "cores, ram_gb, disk_gb, gpu, vram_gb (autoscale probe)",
            "conditions": "list of {type, status, reason, message, lastTransition}",
            "versions": "python + skcapstone versions on the node",
        },
        "conditions": {
            "Ready": "sknoded self-report is alive",
            "MemoryPressure": "free RAM below threshold",
            "DiskPressure": "free disk below threshold",
            "GPUAvailable": "a GPU is present and probed",
            "SyncConflict": "sync-conflict files under the fleet tree (ownership bug)",
        },
        "actions": [
            "skfleet nodes",
            "skfleet describe node <name>",
            "skfleet cordon <name>",
            "skfleet uncordon <name>",
            "skfleet admit <name>",
        ],
    },
    "service": {
        "kind": "Service",
        "description": "A long-running workload (systemd --user unit or Docker container).",
        "spec": {
            "runtime": "systemd-user | docker",
            "unit": "systemd unit name, or container name for docker",
            "replicas": "always 1 in v1",
            "nodeSelector": "label map used by the scheduler (exact match, AND)",
            "tolerations": "list of {key, optional value} tolerating NoSchedule taints",
            "resources": "requested {cores, ram_gb}, advisory, checked as headroom",
            "healthCheck": "{'port': int} tcp probe, or null",
            "restartPolicy": "on-failure (heal with backoff) | never",
            "failover": "manual (default: alert on node-Dead) | auto (re-place)",
            "paused": "bool; true stops healing, never stops the unit",
            "compose": "docker only: {'file': path, 'service': name} for compose",
            "deleted": "tombstone; stops management, never stops the unit",
        },
        "status": {
            "state": "active | failed | inactive | activating | missing | unknown",
            "pid": "main PID when running",
            "since": "ActiveEnterTimestamp (or container StartedAt)",
            "restarts": "heal attempts in the current episode",
            "conditions": "list of {type, status, reason, message, lastTransition}",
        },
        "conditions": {
            "Ready": "unit active and health check (if any) passing",
            "Progressing": "sknoded is actively converging this service",
            "CrashLooping": "bounded restart attempts exhausted; healing stopped",
            "SpecUnverified": "spec/placement signature missing or invalid (Card 3.5)",
        },
        "actions": [
            "skfleet apply -f <file>",
            "skfleet services",
            "skfleet describe service <name>",
            "skfleet reconcile",
            "skfleet drain <node>",
        ],
    },
    "agent": {
        "kind": "Agent",
        "description": "A sovereign agent identity, routed model, and daemon placement.",
        "spec": {
            "name": "agent name",
            "soul": "optional active soul overlay",
            "model": "optional routed model profile",
            "daemon": "optional {node} selector for daemon placement",
        },
        "status": {
            "conditions": "list of {type, status, reason, message, lastTransition}",
        },
        "conditions": {
            "SoulLoaded": "observed active_soul matches spec.soul when set",
            "ModelRoutable": "observed model matches spec.model when set",
            "DaemonReady": "the agent's daemon self-reports ready",
        },
        "actions": [
            "skfleet get agents",
            "skfleet describe agent <name>",
        ],
    },
    "cronjob": {
        "kind": "CronJob",
        "description": "Recurring scheduled work run on a fleet node.",
        "spec": {
            "schedule": "@hourly, @daily, @weekly, or an interval like 30m/2h",
            "command": "shell command to run",
            "nodeSelector": "label map used by selectors (exact match, AND)",
        },
        "status": {
            "lastRun": "ISO8601 UTC timestamp of the most recent run",
            "nextRun": "ISO8601 UTC timestamp of the next scheduled run",
            "conditions": "list of {type, status, reason, message, lastTransition}",
        },
        "conditions": {
            "MissedRun": "the schedule's period plus grace elapsed with no observed run",
        },
        "actions": [
            "skfleet get cronjobs",
            "skfleet describe cronjob <name>",
        ],
    },
    "modelserver": {
        "kind": "ModelServer",
        "description": "A model-serving process exposing ports and loaded models.",
        "spec": {
            "name": "modelserver name",
            "ports": "list of ints in 1..65535 the server should expose",
            "models": "list of model names the server should have loaded",
            "node": "optional node placement",
            "vramBudgetGb": "optional VRAM budget in GB",
        },
        "status": {
            "conditions": "list of {type, status, reason, message, lastTransition}",
        },
        "conditions": {
            "Serving": "every spec port is open and every spec model is loaded",
        },
        "actions": [
            "skfleet get modelservers",
            "skfleet describe modelserver <name>",
        ],
    },
    "config": {
        "kind": "Config",
        "description": "Referenced skvault secret names and expected file hashes for a workload.",
        "spec": {
            "name": "config name",
            "secrets": "list of skvault entry names expected to be present",
            "files": "map of file path to expected sha256 hex digest",
            "rotationDays": "optional max age in days before secrets are overdue for rotation",
        },
        "status": {
            "conditions": "list of {type, status, reason, message, lastTransition}",
        },
        "conditions": {
            "SecretPresent": "every spec secret name is present in the observed skvault",
            "ConfigDrift": "any observed file hash differs from the spec's expected hash",
            "RotationOverdue": "rotationDays is set and the oldest secret exceeds it",
        },
        "actions": [
            "skfleet get configs",
            "skfleet describe config <name>",
        ],
    },
    "operatorapp": {
        "kind": "Operatorapp",
        "description": "A subapp's operator-facet registration and its human-ratified standard actions.",
        "spec": {
            "name": "operatorapp name (the subapp)",
            "cli": "the app's operator CLI entrypoint (e.g. 'skchat operator')",
            "repos": "repos the app's operator facet lives in",
            "contractVersion": "operator-facet contract version the app implements",
            "proposedStandardActions": "actions the app's manifest proposes as reversible standard",
            "ratifiedStandardActions": "HUMAN-only: actions a human has ratified as auto-standard",
            "conditions": "the condition names the app's observe emits",
        },
        "status": {
            "conditions": "list of {type, status, reason, message, lastTransition}",
        },
        "conditions": {
            "ProposalsRatified": "every proposed standard action has been ratified by a human",
        },
        "actions": [
            "skfleet get operatorapps",
            "skfleet describe operatorapp <name>",
        ],
    },
}


def explain(kind: str | None = None) -> dict:
    """Describe registered kinds, or one kind in detail."""
    if kind is None:
        return {"kinds": sorted(KINDS)}
    if kind not in KINDS:
        raise KeyError(f"unknown kind: {kind!r} (known: {sorted(KINDS)})")
    return KINDS[kind]
