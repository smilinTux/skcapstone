# Tailscale endpoint audit

`skfleet node endpoint-audit` is a read-only reconciliation between canonical
fleet node objects and `tailscale status --json`. It does not update fleet
objects, SSH configuration, Tailscale registrations, DNS, or the CMDB.

## Operator use

Run against live local status:

```bash
skfleet node endpoint-audit
skfleet node endpoint-audit --json
skfleet node endpoint-audit --strict
```

Run against retained evidence:

```bash
tailscale status --json > /secure/evidence/tailscale-status.json
skfleet node endpoint-audit --status-file /secure/evidence/tailscale-status.json --json
```

The command matches peers using the canonical node name, declared hostname,
SSH alias, aliases, and declared addresses. It retains only non-secret peer
identity, address, online state, operating system, tag, and last-seen fields in
its report.

`safe_to_route=true` requires exactly one active peer and at least one of that
peer's addresses to match the declared fleet endpoint. A configured address is
never silently replaced with a newly observed value. `--strict` exits 1 when
any matched node is unsafe to route.

## Findings

- `duplicate_tailscale_identity`: more than one peer maps to one fleet node.
- `stale_registration`: an offline registration exists beside an active one.
- `ambiguous_active_endpoint`: multiple mapped peers are online. Routing fails
  closed.
- `configured_endpoint_mismatch`: the one active peer does not own a declared
  endpoint. Routing fails closed.
- `no_active_endpoint`: all mapped peers are offline. Routing fails closed.
- `declared_multi_runtime`: every active peer owns exactly one role-scoped
  `tailscale-windows`, `tailscale-linux`, `tailscale-wsl`, or `tailscale-wsl2`
  address, and the observed operating system agrees with that role. The report
  emits an `active_routes` entry for each peer and routing is safe. This supports
  a Windows workstation whose WSL2 runtime intentionally runs its own Tailscale
  node without treating undeclared dual-active identities as safe.
- `disallowed_peer_os`: an active peer violates the node's explicit
  `spec.tailscale.allowed_os` policy. Routing fails closed.
- `active_peer_limit_exceeded`: active peer count exceeds the node's explicit
  `spec.tailscale.max_active_peers` policy. Routing fails closed. A WSL-only
  workstation declares `allowed_os: [linux]` and `max_active_peers: 1`.

`retirement_candidates` is a plan field, not deletion authorization. Before
retiring a device in the Tailscale admin plane, verify its exact node ID,
hostname, addresses, last seen time, online state, and governing change or
human approval. Capture post-retirement status and rerun the audit.

## chiwk11 qualification evidence

On 2026-08-22, card `e869f7f8` ran the audit against live status. The fleet
object declared `100.66.248.110`. Exactly one online peer matched that address,
node ID `nuFiMs5AL311CNTRL`. The older offline peer
`ndacuY1j6B11CNTRL` at `100.116.214.121`, last seen
`2026-08-21T17:23:22.1Z`, was reported as the sole retirement candidate. The
report marked current routing safe while retaining duplicate and stale warning
findings. The evidence JSON SHA-256 was
`3c2c01acef41f7f02933cea9307a2f424551dd6a845c966d07905c76be9205a3`.

The same run found two active peers for `chiwk12` and marked that node unsafe
to route. That finding is separate from chiwk11 retirement and must not be
resolved by guessing which active identity is canonical.
