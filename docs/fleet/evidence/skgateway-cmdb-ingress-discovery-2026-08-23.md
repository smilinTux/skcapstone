# SKGateway CMDB ingress discovery evidence

## Result

SKCapstone repair card `e841f9d3` replaces blocked candidate `0ffb1d6` after
independent review `cb89b247`. The replacement is based on `origin/main`
revision `213a43ae14dc27089df476c34078b6a4d9038ce9` and is prepared as one clean
immutable code-only candidate.

No live CMDB apply or reconciliation, systemd or firewall mutation, listener
probe, credential access, consumer cutover, or traffic was performed.

## Implemented contract

- TCP observations now fold by host, protocol, and port while retaining a
  sorted unique `bind_addresses` set and bounded `processes` set. Reordered or
  duplicate listener rows produce the same CI ID and attributes.
- A one-address observation retains legacy `bind` and `process` fields.
- Service units retain their historical identity. Non-service unit kinds and
  names that exceed the CMDB slug boundary receive deterministic,
  collision-resistant identities while retaining the exact unit as an alias
  and attribute.
- Declared role graphs permit only socket-to-gateway activation,
  proxy-to-gateway forwarding, and firewall-to-proxy protection. Only the
  socket role binds the declared external port. Generic observed systemd
  dependencies remain `depends_on` and are not relabeled from name hints.
- Local, SSH, and bounded network reconciliation paths use the same extended
  collector tuples.
- `config/cmdb/skgateway-ingress.json` is an optional all-or-nothing declared
  topology contract. It allows exact loopback, private-LAN, and Tailscale
  unicast boundaries and rejects wildcard, unspecified, multicast, link-local,
  container-bridge, public, malformed, zone-scoped, duplicate, incomplete,
  unknown-target, and secret-looking declarations.
- The synthetic chiap01 fixture covers TCP port `28880`, exact loopback,
  `10.0.0.47`, `10.0.0.223`, and `100.80.180.78` binds, and four distinct
  systemd roles. CLI scan is proven read-only and deterministic.
- Governed declarations reject reused endpoints, reused unit ownership,
  duplicate binds and targets, self-targets, invalid role edges, and excessive
  bind counts. Invalid governed input aborts local and network scans before
  partial governed output or reconciliation.

## Files and hashes

| File | SHA-256 |
| --- | --- |
| `docs/tasks/SKGW-CMDB-00-TDD.md` | `b41a888627e14c40ca6b51aea071ab82727c71f59d8aaaa9fd666ce3bf243efd` |
| `src/skcapstone/cli/cmdb.py` | `21e161423e218cb6e4f1f31dc3b710c0e8ece73980467262fc2e17864d41b27d` |
| `src/skcapstone/cmdb_discovery.py` | `2ce24a955d136397874662ff024f62ab2661cf3963e239ef15709e999e71285b` |
| `src/skcapstone/cmdb_ingress_declaration.py` | `6017978d38d604c05ced616ed90a759ab3486af6aacbcca9ce7cc3bec76b3b02` |
| `tests/test_cmdb_ingress_discovery.py` | `ee91849971aab6427c17de32cc3483e2c4b0b13d237c46f8b163b554005ce60b` |
| `tests/fixtures/cmdb/chiap01-skgateway-ingress.json` | `bfc2aecf1ad26fe57c9bb020e9edf377380f4cac78be7b535ac3533150918936` |

## Test evidence

```text
pytest -q tests/test_cmdb_ingress_discovery.py tests/test_cli_cmdb.py tests/test_cmdb.py
76 passed in 1.07s

pytest -q
6403 passed, 38 skipped, 554 warnings in 335.57s

python -m ruff check <changed Python files>
All checks passed

python -m black <changed Python files>
All files formatted

git diff --check
PASS

gitleaks 8.28.0 scoped scan of every changed file
0 findings
```

The skipped full-suite cases are the repository's existing optional or
environment-gated cases. The warnings are pre-existing PGP, cryptography,
Pydantic, and collection warnings and do not originate from this change.

## Compatibility and migration

No CMDB schema, persistent record, migration, dependency version, or installed
`skcoord` source was changed. Existing CMDB and CLI tests pass. Port CI names
remain `host:port`; short service identities remain unchanged even when their
names contain gateway, proxy, ingress, firewall, or nftables terms. Long or
non-service identities retain their exact unit in aliases and attributes.

## Rollback

Revert the single replacement commit containing the two discovery modules,
CMDB CLI facade import and network preflight wiring, task design, fixture,
tests, and this evidence file.
Because no live apply occurred and no persistence schema changed, rollback
requires no CMDB data, service, listener, firewall, or credential operation.

## Known limitation

This card establishes discovery and declaration support only. A separate
governed card remains responsible for supplying a reviewed live declaration
and performing any dry run or reconciliation. Existing ambiguous records are
not renamed, retired, or rewritten by this card.
