# SK MCP topology

SK harnesses should load two MCP servers by default:

- `skcapstone-mcp` owns agent identity, coordination, CapAuth operations,
  SKWhisper context consumption, trust, messaging, and fleet control.
- `skmemory-mcp` owns memory storage, recall, promotion, consolidation, and
  graph operations.

CapAuth and SKWhisper are intentionally not separate default MCP processes.
CapAuth does not ship a standalone MCP entry point; its supported operations
are exposed by `skcapstone-mcp`. SKWhisper is a background context-generation
service whose output is loaded by the SK agent context ritual. Starting it as
an MCP child would duplicate service work and overlap SKMemory's ownership.

Codex reads `${CODEX_HOME:-~/.codex}/config.toml`. Pi uses
`pi-mcp-extension` and reads `~/.pi/agent/mcp.json`. Both configurations must
use absolute executables from `~/.skenv/bin`, start the two default servers
eagerly where supported, and pass through the selected agent environment.

`skcapstone register --env codex` is the declarative reconciliation entry
point. It owns only `approval_policy` and the `skcapstone` and `skmemory` MCP
tables, preserving unrelated user configuration. A dry run reports
`would-create`, `would-update`, or `exists` without writing. Host-specific,
non-secret overrides may be placed in mode `0600`
`~/.config/skcapstone/mcp-host.json` using only these fields:

```json
{
  "approval_policy": "on-request",
  "startup_timeout_sec": 20,
  "tool_timeout_sec": 120,
  "max_stdio_processes": 4
}
```

Secret-shaped or unknown overlay fields are rejected. Credentials must come
from a host-local keyring or process environment and are never rendered into
Codex configuration. The default approval policy is `on-request`; generated
loaders default `SK_CODEX_YOLO` to `0`. Each `skcapstone-mcp` child holds a
host-local advisory slot for its lifetime and refuses starts beyond the
configured ceiling. Slots are released by the operating system when a child
exits, including abnormal exit.

Authentication caches, session databases, client caches and logs, and MCP
slot locks are physical-host live state. They are explicitly excluded by the
bundled `.stignore`; never synchronize an entire `.codex`, ChatGPT application
data, or Windows `OpenAI.Codex` profile between physical hosts.

## Agent selection

Harness launchers resolve identity in this order:

1. `SKAGENT`
2. `SKCAPSTONE_AGENT`
3. `SKMEMORY_AGENT`
4. An explicitly configured `SK_DEFAULT_AGENT`
5. The sole installed agent, if exactly one exists

When multiple agents exist and none is selected, non-interactive launchers
must remain unpinned or fail with guidance. They must never choose an identity
alphabetically or assume `lumina`, `jarvis`, or any other named profile.

System services should use instance templates such as `skcapstone@.service`
and bind all three agent variables to `%i`. Fleet nodes choose their own
instance: for example, Casey's Chi node can run `skcapstone@jarvis` without
changing shared source or synced configuration.

## Desktop deployment

See
[`runbooks/chatgpt-codex-sk-client.md`](runbooks/chatgpt-codex-sk-client.md)
for the Linux and Windows/WSL2 ChatGPT desktop procedure. That runbook records
the accepted four-entry desktop compatibility profile separately from the
two-server default above; it does not make the compatibility profile the
canonical MCP ownership model.
