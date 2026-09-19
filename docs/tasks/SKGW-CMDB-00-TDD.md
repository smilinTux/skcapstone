# SKGW-CMDB-00 task design

## Card

`320bec81`, claimed by `codex-skgw-cmdb-discovery`.

## Objective

Extend SKCapstone's CMDB discovery facade so one TCP port CI preserves every
exact observed bind address, and so systemd gateway, socket, proxy, and
firewall units retain distinct stable service identities and relationship
roles even when their basenames overlap.

## Implementation

- Add a SKCapstone-owned discovery facade over the installed `skcoord`
  collectors without modifying or applying the live CMDB.
- Replace the listener collector with an address-set collector keyed by
  host, protocol, and port. Preserve sorted unique addresses and bounded
  process names while retaining the legacy scalar `bind` for one-address
  observations.
- Replace the systemd collector with a role-aware collector that supports
  service, socket, and timer units. Derive a stable identity from the exact
  unit ID and scope, preserve the normalized unit role, and emit typed
  dependency relationships without folding different unit suffixes.
- Add an optional declared topology file under
  `config/cmdb/skgateway-ingress.json`. Validate its complete shape and emit
  declaration CIs only when every address, network boundary, unit role, and
  relationship is safe.
- Allow only loopback, private-LAN, and Tailscale unicast binds. Reject
  wildcard, unspecified, multicast, link-local, container-bridge, public,
  malformed, zone-scoped, duplicate, and secret-looking metadata.
- Wire CMDB CLI discovery through this facade. Keep scan and reconcile dry-run
  behavior unchanged and leave live apply explicitly gated by `--apply`.

## Tests

- Same host, protocol, and port with multiple IPv4 addresses yields one stable
  CI with a sorted exact address set and no duplicate churn.
- Reordered and duplicate listener lines produce byte-equivalent discovery.
- Same basename service and socket units plus proxy and firewall units yield
  distinct IDs, roles, tags, and typed relationships.
- A chiap01 port 28880 fixture declares exact loopback, LAN, and Tailscale
  addresses and four distinct unit roles.
- Every prohibited address and malformed or secret-looking attribute fails
  closed with no partial declaration.
- Existing CLI, CMDB, migration, reconciliation, and full repository tests
  remain compatible. Formatting, diff, and scoped secret checks pass.

## Acceptance

Discovery preserves multi-address same-port truth with stable identities,
represents all four systemd roles separately, validates the governed ingress
boundary, and produces a deterministic dry-run fixture. No live CMDB, service,
listener, consumer, firewall, or credential state is changed.

## Rollback

Revert the facade, CLI import, tests, fixture, and this task design. The
installed `skcoord` package and live CMDB remain unchanged, so no data or
runtime rollback is needed.

## Prohibited

No live CMDB reconciliation or apply, no systemd or firewall mutation, no
listener probe outside synthetic runners, no credential or secret access, no
consumer cutover, and no protected traffic.
