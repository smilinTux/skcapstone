# Native source custody acceptance

Card: bd84af1e. Owner: codex-production-builder-bd84af1e.
Claim: 032cc63a9cca4fd58ac944f839ec17e4.
Parent: 9b230773. Source-only local commits are explicitly authorized.

This is the first of two additive source candidates for the same claimed leaf.
Its prerequisite is 31feb0ed17db9b5d2147cc9b64fcc3f5f9c70c00, the isolated
composition of the approved production baseline, shared policy and root
dispatcher changes through 53514f6e, and dynamic route resolver 7ea522ba.
The second candidate applies these helpers to the remote builder queue.

## Changes and acceptance

- source_bundle validates the native current producer owner and claim,
  typed PASS_FOR_REVIEW, claim timestamp, repository/base, candidate head,
  tree, branch and committed completion evidence. A read-only, networkless
  bwrap namespace exports an incremental Git bundle bounded to 8 MiB.
- Immutable bundle, manifest and evidence bytes use content hashes and
  private atomic publication. Reconciliation reuses an acknowledged exact
  candidate instead of repeatedly exporting or uploading it.
- source_transport transfers only one selected source-only card/head packet
  over existing trusted SSH. The authority validates its own current typed
  native source claim before retaining a producer upload. Reviewer fetches
  require the exact current source-only head. No broad evidence replication,
  credential copying, remote Git push or Fiber runtime dependency is added.
- source_access preserves the existing exact card-read command and the
  stricter active source-only claim mode. It additionally accepts exactly one
  literal native artifact command. It never passes arbitrary Python, shell,
  extra arguments or packet commands through to execution.
- Reviewer preflight checks exact artifact bindings. Import builds a new
  private checkout, fetches the historical base, verifies bundle digest,
  advertised ref/head, tree and ancestry, then checks out the exact head.
  Existing workspaces are refused and retained.
- The local worker wrapper accepts explicit source repository/base arguments.
  Production source publication happens only after the existing exact-child
  and cgroup terminal proof. Source claims remain held for independent review
  or evidence recovery, and legacy source cleanup/finalization is bypassed.
  Reviewer and legacy paths keep their existing completion behavior.

## Verification

The focused artifact and wrapper suite passes 95 tests with zero skips.
The combined production candidate suite passes 325 tests with zero skips.
Exact immutable commit/tree/file hashes and test log hashes are recorded in
the adjacent operator CANDIDATE-ARTIFACT.json descriptor, outside this commit
to avoid embedding a commit's own hash.

Commands use PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=src and the existing
~/.skenv/bin/python -m pytest -p no:cacheprovider -q with:

```
tests/fleet/test_source_bundle.py
tests/fleet/test_source_transport.py
tests/fleet/test_source_access.py
tests/fleet/test_production_exit.py
tests/test_skfleet_worker_exit_evidence.py
tests/test_terminal_capacity.py
```

Tests use temporary native stores and real isolated Git export/import, plus
mocked SSH transport and service boundaries. They cover changed claims,
source bytes, evidence, head/tree/ref, symlinks, malformed or unrelated
commands, unsupported packet actions, privacy labels, lost process proof,
immutable retries and preservation of existing workspaces.

## Deployment and limitations

No source, service, private configuration or SSH key deployment has occurred.
Independent review, root composition, reversible installation and a real
production trial remain required. Keep the claim until those gates pass.

The historical bundle base must be fetchable using the existing repository
access. An unpublished baseline needs explicit qualified priming; this helper
does not pretend to transport arbitrary missing history. Bundles larger than
8 MiB and unavailable user namespaces are refused.

chiwk12/13 do not share all evidence or native queue paths. Their selected
artifact transport uses the existing restricted authority SSH identities;
the reviewed native forced-command target must be installed before use.
Original public keys, from restrictions and restrict options must remain
unchanged. ZIO retains its stricter source-claim read mode. Configuration
qualification and native queue delivery are tracked separately under c199.

Per-response provider attribution and protected Matter authorization are not
established by this infrastructure change. Source-only engineering scope,
native independent review and required tests remain authoritative.

Rollback is a hash-fenced restoration of installed file preimages and exact
forced-command target preimages during an idle maintenance window. Preserve
all source bundles, native events, candidate workspaces and active claims.
