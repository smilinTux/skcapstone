# SKGateway CMDB ingress discovery evidence

## Result

SKCapstone card `320bec81` completed with result `PASS_CODE_ONLY` in clean
worktree `/home/skuser01/worktrees/skcapstone-skgw-cmdb-00` at base revision
`213a43ae14dc27089df476c34078b6a4d9038ce9`.

No live CMDB apply or reconciliation, systemd or firewall mutation, listener
probe, credential access, consumer cutover, or traffic was performed.

## Implemented contract

- TCP observations now fold by host, protocol, and port while retaining a
  sorted unique `bind_addresses` set and bounded `processes` set. Reordered or
  duplicate listener rows produce the same CI ID and attributes.
- A one-address observation retains legacy `bind` and `process` fields.
- Gateway, socket, proxy, and firewall systemd units receive identities that
  include host, scope, and exact unit suffix. Generic systemd services retain
  their historical identity to prevent unrelated CI churn.
- Socket activation, proxy forwarding, firewall protection, dependencies,
  host placement, and port binding have explicit relationship types.
- Local, SSH, and bounded network reconciliation paths use the same extended
  collector tuples.
- `config/cmdb/skgateway-ingress.json` is an optional all-or-nothing declared
  topology contract. It allows exact loopback, private-LAN, and Tailscale
  unicast boundaries and rejects wildcard, unspecified, multicast, link-local,
  container-bridge, public, malformed, zone-scoped, duplicate, incomplete,
  unknown-target, and secret-looking declarations.
- The synthetic chiap01 fixture covers TCP port `28880`, three exact bind
  addresses, and four distinct systemd roles. CLI scan is proven read-only and
  deterministic.

## Files and hashes

| File | SHA-256 |
| --- | --- |
| `docs/tasks/SKGW-CMDB-00-TDD.md` | `fb2db9806bd3d5d7a41316d1fc64c85b5c01547801efc9f9dc7fc693241f4192` |
| `src/skcapstone/cli/cmdb.py` | `2586bf2613fc9f4aabe77a3063447e86fc34a09cb25e6f7fe87fee7e02360c08` |
| `src/skcapstone/cmdb_discovery.py` | `f298133446872fba1bed7576a30d3ce88b8ac163e8a4b5fda667fbc868fd487a` |
| `src/skcapstone/cmdb_ingress_declaration.py` | `69a4f5996c401d8af3f4eb4d330bc4a1d0433b03bb0e00332f850cd8ac302f3e` |
| `tests/test_cmdb_ingress_discovery.py` | `5b9ee2c42087f3e1bf9cfa5f39295ff21d97681b25100313365058965836626e` |
| `tests/fixtures/cmdb/chiap01-skgateway-ingress.json` | `f09176448e88143ebb2ab6c8336fcefa560020c7991ac09f9839e14cbdabd97b` |

## Test evidence

```text
pytest -q tests/test_cmdb_ingress_discovery.py
23 passed in 0.27s

pytest -q tests/test_cmdb_ingress_discovery.py tests/test_cli_cmdb.py tests/test_cmdb.py
62 passed in 0.56s

pytest -q
6389 passed, 38 skipped, 554 warnings in 344.23s

python -m ruff check <changed Python files>
All checks passed

python -m black <changed Python files>
All files formatted

git diff --check
PASS

scoped detect-secrets scan of every card file
0 findings
```

The skipped full-suite cases are the repository's existing optional or
environment-gated cases. The warnings are pre-existing PGP, cryptography,
Pydantic, and collection warnings and do not originate from this change.

## Compatibility and migration

No CMDB schema, persistent record, migration, dependency version, or installed
`skcoord` source was changed. Existing CMDB and CLI tests pass. Port CI names
remain `host:port`; generic systemd service identities remain unchanged. The
new exact identities are restricted to the ingress roles that previously
folded ambiguously.

## Rollback

Revert the two discovery modules, the CMDB CLI facade import and network
collector wiring, the task design, fixture, tests, and this evidence file.
Because no live apply occurred and no persistence schema changed, rollback
requires no CMDB data, service, listener, firewall, or credential operation.

## Known limitation

This card establishes discovery and declaration support only. Card
`a3ff6791` remains responsible for supplying the reviewed live declaration and
performing a separately governed dry run or reconciliation. Existing ambiguous
records are not renamed, retired, or rewritten by this card.
