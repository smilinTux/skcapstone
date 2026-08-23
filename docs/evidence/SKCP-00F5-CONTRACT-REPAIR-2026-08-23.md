# SKCP-00F5 contract repair evidence

Date: 2026-08-23
Card: `057f981b`
Branch: `codex/skcp-00f5-contract-repair-v2-20260823`
Base: `origin/main` at `e12a2582aa8733a00b2c8238f722483d47962191`
Worktree: `/home/skuser01/work/skdashboard-skcp00f5-v2-20260823`
Status: candidate repair pending independent review and publication gates

## Scope

This candidate repairs only the reconstructed V1.1.0 contract set, its V2
compatibility note, and the dedicated contract remediation tests. Frozen
manifests, schedule artifacts, wireframes, original contract files, runtime
code, board state, and secret-baseline files are outside the change.

The predecessor stable V1.1.1 manifest remains an audit input and is not
rewritten. Its exact SHA-256 is
`2876a22ea8fe29fb28c8c2c918c9e67b339e9f7836e59218b9bac1dba573dbe0`.

## Repaired invariants

- Metric and projection contracts carry a required typed `visibility` object
  with authorization state. Policy-filtered, unauthorized, and redacted
  records cannot map access decisions to `truth_state: not_applicable` or
  expose a non-null value.
- Report quality statements preserve visibility separately from source truth.
- Proposal insights require nonempty summary, evidence references, calculation
  references, uncertainty, policy decision reference, and model provenance.
- Abstained insights require a structured nonempty abstention reason and cannot
  expose a `preview_command` next step.
- Ready sensitive action previews require at least one approval, and every
  approval must be approved, current, and exact-version-bound. Mixed rejected,
  expired, unresolved, or stale entries fail validation. Every preview has a
  nonempty policy decision reference.
- The compatibility note records the V2 normalization and reserves
  `not_applicable` for explicit scope semantics rather than authorization.

## Verification

The dedicated remediation suite includes positive and negative fixtures for
each predecessor counterexample and sensitivity checks that fail if the
corresponding guard is removed. It also validates every reconstructed schema
with Draft 2020-12 and local references.

Results before publication:

- Focused remediation suite: `21 passed, 2 warnings`.
- Full suite: `266 passed, 143 warnings`.
- Draft 2020-12, filename/ID/version, and local reference checks: `4 passed,
  17 deselected, 2 warnings`.
- Original contract architecture and pinned original artifact hashes: `8
  passed`.
- Ruff: `All checks passed!`.
- Forbidden em/en dash scan: clean.
- `git diff --check`: clean.

The reconstructed V1.1.0 artifact hashes are:

```text
control-plane-action-preview.v1.1.0.schema.json 24befe22392ec22baf6056a828cd519e2c3cad2b9b7ca7a6f5e057b8647319dc
control-plane-insight.v1.1.0.schema.json e1668f7164e600be4bfe73f02143c2d5ab79ccf4096dde96c80ae78e9216ce10
control-plane-metric-result.v1.1.0.schema.json 2ea8a95266a16923d85e5e6364dedf2978ce9f514742e99cdac83c3fcc150a19
control-plane-recommendation.v1.1.0.schema.json d9a80faae276d2ce22a0189c1a03081dd2b293618e88838612df16895dd49fcd
control-plane-report-snapshot.v1.1.0.schema.json f8058031ec7fdf561107792bbf1e690a12f3fbcaffe6b9c1926a9fa57b64dfa6
openapi.control-plane.v1.1.0.json 3a3d61c792ab9bafeed28f17df15eb7ed38f6ba50b057facd138f61ad6e2456e
```

The compatibility note hash is
`a5b9ce5875b692e9035907cf516048dc18677ab43ead5a2757d13cffc7718b33` and
the dedicated test hash is
`eac09d0d86ee14d742f66a5398e6c58561b3f4d3703e64f42b5a35fc415b3db9`.
The publication receipt will add the immutable commit, PR, CI, and board
readback after review.

## Non-authorization and rollback

This evidence authorizes no implementation runtime, data access, deployment,
provider request, external action, board mutation, or schedule change. The
repair is reversible by reverting the candidate commit or restoring the
pre-repair bytes of the scoped contract, note, test, and evidence files.
