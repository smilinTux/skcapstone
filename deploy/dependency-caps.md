# Upper caps on third-party libraries in SK packages

Rule (Chef, 2026-11-15): an SK package may not put an upper bound on a
third-party library (`<X`, `<=X`, `~=X`, `==X`) without a written reason and a
test that shows the bound is needed. Every cap that exists is a row in the
table below. A cap pulls the whole shared venv down with it: skbrain's
`click<8.2` and `typer<0.13` moved every chi host from click 8.4 to 8.1.8 and
broke huggingface-hub (skcapstone #1382), and the `rich<14` caps broke twine 7.

How it is enforced:

- **CI gate** (`.github/workflows/fleet-lock.yml`, job `caps`): installs the
  fleet lock in a fresh venv and runs `scripts/ci/fleet_lock_gate.py caps`. Any
  upper bound an installed SK package puts on a third-party library that is not
  a row here fails the PR. A row whose cap no installed package carries any
  more prints as `stale`; delete it in the next PR that touches this file.
- **Renovate** (`smilinTux/renovate-config`, preset `caps`): when a release
  above a cap exists, Renovate opens a monthly "try lifting this cap" PR in the
  package's repo. The PR's CI is the test: green means the cap goes and the row
  is deleted here; red means the row stays and `last checked` moves.
- SK-internal bounds (`capauth<0.4`, `skharness==0.3.45`) are release ordering
  inside the estate, not third-party caps, and are not tracked here.

Status values: **proven** (a test shows the bound is needed), **pin** (an
exact pin with a recorded reason), **dev-only** (a lint or test tool pin in a
dev extra, never in the fleet venv), **open** (no recorded reason; not binding
today because no release above the cap exists, or binding only outside the
fleet lock; remove at the package's next release).

The parser reads the first three columns; keep the cap in backticks.

| Package | Dependency | Cap | Status | Reason | Test that proves it | Last checked |
| --- | --- | --- | --- | --- | --- | --- |
| skcapstone | mcp | `<2.0` | proven | mcp 2.x removed `Server.list_tools`, which the MCP server registers its handlers with. | `import skcapstone.mcp_server` on mcp 2.3.0: `AttributeError: 'Server' object has no attribute 'list_tools'`; the gate's smoke imports it on every lock PR. | 2026-11-15 |
| skmemory | mcp | `<2.0` | proven | Same `Server.list_tools` removal. | `import skmemory.mcp_server` on mcp 2.3.0 fails as above; gate smoke. | 2026-11-15 |
| sksecurity | mcp | `<2.0` | proven | Same `Server.list_tools` removal. | `import sksecurity.mcp_server` on mcp 2.3.0 fails as above; gate smoke. | 2026-11-15 |
| skchat-sovereign | mcp | `<2.0` | proven | Same `Server.list_tools` removal (dev extra; the runtime imports mcp unconditionally). | `import skchat.mcp_server` on mcp 2.3.0 fails as above; gate smoke. | 2026-11-15 |
| skskills | mcp | `<2.0` | proven | The aggregator registers with `Server.list_tools`, gone in mcp 2.x. | skskills test suite on mcp 2.3.0: `AttributeError` in the aggregator test (skskills #12). | 2026-11-15 |
| skdashboard | mcp | `<2.0` | open | No MCP server module found to probe; presumed the same API. | none yet: lift PR will tell. | 2026-11-15 |
| skcapstone | ansible-core | `==2.21.3` | pin | The sealed qualify env (`data/qualify-env.json`) builds this exact ansible-core for Python >= 3.12. | `tests/fleet/test_qualified_runtime_dependencies.py`. | 2026-11-15 |
| skcapstone | ansible-core | `==2.19.11` | pin | Same, for Python < 3.12 (2.21 needs 3.12). 2.19.11 fixes CVE-2026-11332 (GHSA-w8p5-mx5w-cpqj, `ansible-galaxy role install` argument injection), which 2.19.5 carried; the 2.21.3 pin was never affected (fixed in 2.21.1). No fleet host runs Python < 3.12. | `tests/fleet/test_qualified_runtime_dependencies.py`. | 2026-11-15 |
| sksecurity | cryptography | `<51.0.0` | open | Major-version guard on a crypto library; no test. Not binding: 50.0.2 is the newest release. | none: lift PR when 51 ships. | 2026-11-15 |
| skcomms | cryptography | `<51.0` | open | Same major-version guard (pqc, nostr, all and dev extras). Not binding. | none: lift PR when 51 ships. | 2026-11-15 |
| skcomms | fastapi | `<1.0.0` | open | Pre-1.0 guard (api, all and dev extras). Not binding: newest is 0.163.1. | none. | 2026-11-15 |
| skseal | fastapi | `<1.0` | open | Pre-1.0 guard. skseal is not in this lock. | none. | 2026-11-15 |
| cloud9 | click | `<9.0.0` | open | Major guard, not binding (newest 8.5.1). | none. | 2026-11-15 |
| cloud9 | pydantic | `<3.0.0` | open | Major guard, not binding (newest 2.14.3). | none. | 2026-11-15 |
| skcomms | pydantic | `<3.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skcomms | pyyaml | `<7.0` | open | Major guard, not binding (newest 6.0.3). | none. | 2026-11-15 |
| skcoord | pydantic | `<3.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skmemory | click | `<9.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skmemory | pydantic | `<3.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skmemory | pyyaml | `<7.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skmemory | psycopg | `<4.0` | open | Major guard (pg and all extras), not binding (newest 3.3.9). | none. | 2026-11-15 |
| sksecurity | click | `<9.0.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| sksecurity | pydantic | `<3.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skseed | click | `<9.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skseed | pydantic | `<3.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skseed | pyyaml | `<7.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skseal | click | `<9.0` | open | Major guard, not binding. skseal is not in this lock. | none. | 2026-11-15 |
| skseal | pydantic | `<3.0` | open | Major guard, not binding. | none. | 2026-11-15 |
| skseal | pgpy | `<1.0` | open | Pre-1.0 guard, not binding (newest 0.6.0). | none. | 2026-11-15 |
| skseal | aiofiles | `<26.0` | open | Major guard, not binding (newest 25.1.0). | none. | 2026-11-15 |
| skseal | rich | `<15.0` | open | BINDING where skseal runs (not in this lock): rich 15.0.0 exists. No recorded reason. | none: remove at skseal's next release. | 2026-11-15 |
| skseal | pypdf | `<7.0` | open | BINDING where skseal runs (not in this lock): pypdf 7.1.0 exists, and pypdf < 6.21.4 carries 100+ advisories. No recorded reason. | none: remove at skseal's next release. | 2026-11-15 |
| skos-sovereign | click | `<8.2` | open | BINDING where skos runs (not in this lock): the cap skbrain was extracted with. No test needs it (skbrain 1.0.2 dropped it). | none: remove at skos's next release. | 2026-11-15 |
| skos-sovereign | typer | `<0.13` | open | BINDING where skos runs (not in this lock): same as above. | none: remove at skos's next release. | 2026-11-15 |
| sksecurity | ruff | `<0.17` | dev-only | Lint reproducibility in the dev extra. | n/a. | 2026-11-15 |
| skseed | ruff | `==0.16.3` | dev-only | Lint reproducibility in the dev extra. | n/a. | 2026-11-15 |
| cloud9 | ruff | `<0.17` | dev-only | Lint reproducibility in the dev extra. | n/a. | 2026-11-15 |
| cloud9 | black | `<27.0` | dev-only | Format reproducibility in the dev extra. | n/a. | 2026-11-15 |

## Removed 2026-11-15 (no test needed them)

- skbrain: click `<8.2`, typer `<0.13` (skbrain 1.0.2, skbrain #4). typer's floor rose to 0.16: older typer fails 13 CLI tests on click >= 8.2.
- skskills: rich `<14.0`, click `<9.0`, pydantic `<3.0`, pyyaml `<7.0` (skskills 1.2.1, skskills #12).
- skcapstone: rich `<14.0`, click `<9.0`, pydantic `<3.0`, pyyaml `<7.0` (this repo, the fleet-lock PR).
- skchat-sovereign: fastapi `<0.136` (skchat-sovereign 0.14.267, skchat #196). Tests now walk nested routers.
