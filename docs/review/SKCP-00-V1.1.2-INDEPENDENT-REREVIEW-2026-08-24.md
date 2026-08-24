# SKCP-00 V1.1.2 independent rereview

Review card: `d0edbff1`
Reviewer: `codex-skcp-00r-v112-rereviewer`
Review target: SKDashboard release `v0.1.25` at
`1ee7e75833840a52c734ecfb7635b250c4bedb9e`
Approval record revision: `3e443a79683995d3d27b5f410788f4335ec41ccf`
Verdict: **FAIL**

This review did not repair or modify any candidate artifact. The only new file
is this independent review record.

## Blocking finding

### R1: the sealed lineage narrative contradicts the sealed lineage bytes

The manifest pins
`docs/architecture/ADR-0001-CONTROL-PLANE-MEASUREMENT-AND-REPORTING-v1.1.2.md`
at SHA256
`fb3f5668e4d7d5c8db82bb6a2e74821944d91a9fec3c3db9b2dc74b58ab1ff0e`.
Lines 56 through 60 of that exact artifact state that the two historical PNG
wireframes are unavailable lineage inputs and that no PNG bytes were copied by
another write method.

That claim conflicts with the exact reviewed tree. Commit
`1ee7e75833840a52c734ecfb7635b250c4bedb9e` adds both PNG files under the
lineage directory, and the candidate test at
`tests/test_control_plane_v1_1_2_candidate.py` lines 91 through 98 requires
their exact archived bytes:

- `docs/review/lineage/v1.1.0/docs/wireframes/control-plane-estate-pulse-v2.png`
  has SHA256
  `33c400d4d4546e120a2662d5ef887d27ee85e4b87f5bdd973e038114d5e8c129`.
- `docs/review/lineage/v1.1.0/docs/wireframes/control-plane-authorization-preview-v2.png`
  has SHA256
  `f1ddf830f41a052917aeab6640183f649c0c8937cf7c441c5f2d1ef3d87463a8`.

The F7 evidence file independently says the candidate preserves those PNG
bytes under lineage paths. The approved exact-hash package therefore contains
two incompatible descriptions of the same lineage state. A candidate presented
as the truthful superseding record cannot pass while its pinned ADR says bytes
are unavailable and were not copied but its exact tree and tests prove they are
present and archived.

Required disposition: create a new superseding exact-hash candidate that makes
the lineage narrative agree with the retained bytes, obtain a new exact-hash
human approval, and repeat this independent review. Do not edit the approved
manifest or receipt in place.

## Exact-hash and lineage recomputation

- Manifest SHA256:
  `257db46aa26297873cd6a769e3f0eb7e6e3cf756224f99ef9a3aad61a45ff5ab`.
- Detached receipt SHA256:
  `46b98341094cf06a5f260c0ad1eed1e8d3a0090f27c2f8d570dcb84312028749`.
- Receipt `manifest_sha256` matches the manifest, is non-recursive, and the
  receipt is absent from the manifest artifact list.
- All 18 manifest artifact hashes match.
- The three predecessor manifest hashes match
  `88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`,
  `6b35f9e77f8f51dde5243bd9ebc5f55adbf65141d344218b165845bd3475a194`,
  and
  `2876a22ea8fe29fb28c8c2c918c9e67b339e9f7836e59218b9bac1dba573dbe0`
  exactly.
- The seven archived V1.1.0 contract hashes match their historical expected
  values and are byte-distinct from the seven active repaired contract files.
- The archived F3F evidence hash matches
  `f4e7e4404196da0fa43dd3fce2938d0ff9a36137254e706093d23f439ab16fae`.
- The capture canonical subset recomputes to
  `af66e566f71a896a07c1c3403e3dd99442fd660684fa7b5b3e49f56764040b2a`.
- All 25 JSON documents parse and every local JSON reference resolves.

## Contract and decision-boundary challenge

The repaired contract set at
`dcdd6b25df3663656e7d476ac848ffdf6e183c66` passes its fail-closed checks:

- Unavailable, unreachable, unknown, and not-applicable metrics reject numeric
  and textual values. Zero requires nonempty evidence and watermarks. Current
  metrics and reports reject source errors.
- Visibility and source truth are separate. Policy-filtered and unauthorized
  data cannot be mapped to not-applicable health.
- Proposed insight and recommendation objects require metric, evidence,
  calculation, uncertainty, policy, model provenance, best-practice version,
  impact, counter-indicators, alternatives, risks, and preconditions as
  applicable. Insufficient evidence produces a typed abstention.
- Canonical metrics are calculated by the deterministic engine before model
  access. The insight boundary receives permitted results, has no command
  execution path, and returns no bearer capability material.
- Action preview and action authorization are separate. Ready mutating previews
  require a nonempty exact target and expected version, current exact-version
  approvals, policy evidence, expiry, verification, rollback, and owner
  revalidation.
- Counterexample sensitivity tests prove the relevant schema guards are active
  rather than vacuous.

## Gates, parity, and non-authorizations

Fetched main ancestry is:

`dcdd6b25` F8, then `ed4b68fe` CI repair, then `1ee7e758` F7, then
`3e443a79` exact-hash approval.

Live folded board readback shows `9442b3b3`, `26c69f86`, `ef91a99f`, and
`bea13a70` done, while `d0edbff1` remains review. Its effective dependencies
are `9442b3b3` and `bea13a70`. The four schedule leaves and legacy SKCP-01,
SKCP-02, and SKCP-07 retain direct or transitive paths through `d0edbff1`.

The candidate preserves historical parity as 985 checked, 590 matched, 125
mismatches, 270 missing, and open drift 10. Its captured fresh observation is
explicitly unsafe. A new read-only observation returned 1069 checked, 668
matched, 131 mismatches, 270 missing, and open drift 10 against threshold 5.
That later change is expected board evolution and does not rewrite the frozen
capture. No reconciliation was run.

The candidate and human approval preserve the stated non-authorizations. This
review performed no implementation, deployment, activation, restart, external
action, protected Matter access, HammerTime Inbox access, board reconciliation,
service change, Atlas change, gate bypass, or candidate repair.

## Verification results

- Focused contract, candidate, and approval tests: `38 passed`.
- Full dashboard suite: `309 passed`, `143 warnings`.
- Ruff over `src/` and `tests/`: passed.
- Candidate-scoped forbidden dash scan: passed.
- `git diff --check`: passed before this review artifact was added.
- PR 28 for F8, PR 31 for the CI repair, PR 30 for F7, and PR 32 for human
  approval were merged with their required checks successful.

These successful checks do not resolve R1 because the test suite currently
asserts the archived PNG bytes without challenging the contradictory ADR text.

## Decision

`FAIL`. Leave `d0edbff1` incomplete and preserve its downstream gates.
