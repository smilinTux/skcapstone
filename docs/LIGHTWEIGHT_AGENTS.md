# Lightweight Fleet Agents

Fleet operators sometimes need a role agent - a worker that implements cards, a
reviewer that verifies them - without the full sovereign pillar stack. The
interactive `skcapstone init` / `skcapstone onboard` wizard targets sovereign
agents and cannot run unattended; `skcapstone agent profile --init` refuses to
run without an existing home. The non-interactive provisioning path closes that
gap.

## Provisioning

```bash
skcapstone init --non-interactive --name veritas --role reviewer
```

This is scriptable: no prompts, exit code 0 on success, non-zero on any error.
Options:

| Flag | Effect |
|---|---|
| `--name NAME` | Required. Display name; slugified for the directory. |
| `--role ROLE` | `worker` (default), `reviewer`, or a custom slug. Picks the MANDATE.md template. |
| `--mandate "TEXT"` | Custom mandate text for identity.json and MANDATE.md. |
| `--no-mandate` | Skip writing MANDATE.md. |
| `--home PATH` | Shared root override (default `~/.skcapstone`). The agent lands at `<home>/agents/<slug>/`. |
| `--force` | Overwrite an existing lightweight profile. Without it, re-provisioning an existing agent fails. |

## What gets written

Modeled on the hand-built reference profile at `~/.skcapstone/agents/veritas/`:

```
~/.skcapstone/agents/<slug>/
├── identity/
│   └── identity.json   # name, role, mandate, capauth_managed: false, profile: "lightweight"
├── profile.yaml        # bridge-curation block (same shape as `agent profile --init`)
└── MANDATE.md          # role mandate template (optional)
```

`profile.yaml` is written in the exact shape the Telegram bridge and
`skcapstone agent profile --agent <slug>` read, so a lightweight agent is a
first-class citizen of the per-agent tooling from the moment it exists.

## Capability delta: lightweight vs sovereign

A lightweight profile deliberately does **not** get:

| Subsystem | Sovereign (wizard) | Lightweight | Consequence |
|---|---|---|---|
| PGP identity (capauth) | Keypair generated | `capauth_managed: false`, no keys | Cannot sign/encrypt, no DID, no capability tokens |
| Memory (skmemory) | short/mid/long-term layers, seed import | none | No persistent memory, no ritual, no memory MCP tools |
| Trust (Cloud 9) | FEB chain, trust rehydration | none | No trust scoring or entanglement state |
| Soul | blueprint + overlays | none | No persona; system prompts fall back to defaults |
| Security (sksecurity) | audit log, threat detection | none | No audit trail of its own |
| Sync / mesh | Syncthing folders, seed push/pull | none | State does not roam to other nodes |
| Heartbeat / board registration | first beacon + agent file | none | Register separately if the agent should appear on the board |

What a lightweight profile **does** get: a resolvable agent home (so
`resolve_agent_home`, `agent profile`, and the bridge work), a recorded
identity and mandate for provenance, and a bridge-curation block.

## Upgrading to sovereign later

A lightweight agent can be upgraded in place; nothing about the scaffold is
one-way:

1. Point the interactive wizard at the agent home:
   `SKCAPSTONE_HOME=~/.skcapstone/agents/<slug> skcapstone onboard`
   (or `skcapstone init --home ~/.skcapstone/agents/<slug>`).
   The wizard fills in the pillar directories (identity keys, memory, trust,
   security, sync) around the existing files.
2. Refresh the bridge block afterward if desired:
   `skcapstone agent profile --agent <slug> --init`.
3. Remove or update the `"profile": "lightweight"` marker in
   `identity/identity.json` once capauth manages the identity
   (`capauth_managed` flips to `true` when the wizard generates keys).

The reverse (sovereign to lightweight) is not supported: a sovereign home has
live key material and state that the lightweight layout does not model.
