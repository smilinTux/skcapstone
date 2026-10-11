# SEP21 legacy review routing evidence

Base: `2e489e417a45579fdc4283535becd199d8e806bd` (origin/main).
Branch: `fix/sep21-review-route-evidence`.
First commit: `151d32249f25493e52c45542430ec88e706bf3b5` (tests first).

## Root cause

`scripts/fleet/skfleet-rotate.py:evaluate_review_capacity` emits
`source-provider-evidence-required` when `independent_review_routes` raises.
`src/skcapstone/fleet/production_review.py:producer_family` wants either an
encoded provider identity, such as pi-codex, or an authoritative typed provider.
The rotate wrapper supplies only the producer identity. Adding an invented
provider field to a card would neither establish attribution nor repair this
caller. The historical receipts identify jarvis or exact candidate bytes;
they do not establish a served provider. No provider evidence was created.

`review_dispatch._source` separately requires the modern sealed source manifest
and producer custody/outcome. A released legacy source cannot meet that contract.
`partition_remote_reviews` sends source-only seat-seraph reviews to that path.

## Minimal legitimate change

Keep native remote custody intact. The existing local hosted review path may
use a verified legacy source binding instead of inferring a provider family:

- Explicit review and review-distinct-agent labels, without source-only.
- Unowned source in review, with no conflict, hold or archive.
- Matching repository, base ref/revision and canonical hosted PR on both cards.
- Latest native PASS_FOR_REVIEW written by the declared producer, exact head
  and tree, with no later claim, mediated contrary verdict or modern claim fence.
- No modern source-bundles directory, including malformed or dangling entries.
- Actual owned regular candidate bytes match the native verdict hash.
- Actual source receipt bytes match the source link and review evidence hash.
- Distinct reviewer principal, qualified enabled routes and all existing
  capacity, size, lane, claim, hosted-CI and terminal publication gates remain.

Old evidence files use group-write mode. This verifier hashes bounded owned
regular files without modifying them. Board-pinned hashes provide integrity;
modern source-bundle permission checks are unchanged. Missing, redirected,
changed or oversized bytes refuse. This is routing eligibility, not a review
verdict, provider attribution, hosted CI result or remote launch authorization.

## Cards and audited metadata corrections

All reads used `skcapstone coord show <id> --json`. All writes used
`--agent lumina-nor`, followed by readback.

| Review | Source | Findings and action | Readback |
| --- | --- | --- | --- |
| 497f0f02 | 6d8c4727 | Bound existing source PR #18 to the review. Exact native head acdae00a and receipt hash verify. | Yes |
| f519c41a | 9dee7ad3 | Replaced prose PR reference with source PR #14 URL. Removed contradictory source-only label: its existing contract requires hosted CI and PR review. Exact native head ac14cc04 and receipt hash verify. | Yes |
| b3416bde | ca220939 | No write. Already has an independent PASS for historical head 42c37e6b. Latest source verdict instead binds 340d9b7e. Do not authorize a fresh review by pretending this is the current candidate. | Read only |

Exact evidence bindings:

- 497f0f02: head `acdae00a4dcaca5aabf1ca8c462c7e89a06bd050`, tree
  `116868c3b90990243cd42a15ececc1cb5c8a6757`, receipt
  `092318e5984302273154ce8c32087232c5b9a8f4f5da08a6298a61b652b17a83`.
  Native bundle SHA256:
  `d532dddb5cfda29513ffd742ed6db384802f5d9552a0d8228cba6f7488a841ac`.
- f519c41a: head `ac14cc040cbf30628b0e9d8910e7a646c4599b58`, tree
  `fbd53864a5c3cc184a614a3525b56ac1da660a46`, native receipt
  `9967025d9128d480fcc15bacfcd769a6e54179844b3a5a10e02f8f9e9c296592`.
- b3416bde: existing independent review SHA256
  `34ab48eedfb5cd0a238d40e95a00d9c58db80ebf0aab3e29979b6a3b5952d6dd`
  verified from bytes. It names reviewer pi-seraph-b3416bde and preserves the
  SOURCE_ONLY_SYNTHETIC scope. Historical producer receipt SHA256
  `acac0bc3cc72f2e6d82910277d2ac8d149b4a7cc417e32dd988cfa7ea55d478d`.

Read-only execution of the proposed verifier against current cards:
497f0f02 true, f519c41a true, b3416bde false. All three were unowned in review.
No dispatcher cycle or worker was run by this task. No deployment or merge.
The two corrected reviews still require this PR to reach deployed code and
then must pass ordinary admission, execution and publication checks.

## Validation

Before implementation, the new proof tests failed collection because the
legacy verifier did not exist. An additional routing regression failed on
same-principal refusal semantics; the test was corrected to expect the existing
ValueError refusal rather than an empty route list. Source-only refusal was
then enforced explicitly on the verified-legacy route input.

Final command (PYTHONPATH=src):

```
pytest -q tests/fleet/test_legacy_review_source.py \
  tests/fleet/test_production_review.py tests/fleet/test_production_dispatch.py \
  tests/fleet/test_remote_review_dispatch.py tests/fleet/test_review_source_revision.py \
  tests/fleet/test_production_hosted_review_identity.py tests/fleet/test_production_brief.py
```

Result: 175 passed in 11.08 seconds. Includes 29 legacy proof cases, the actual
rotate capacity helper and all three caller bindings, distinct-principal and
explicit GLM policy checks, modern remote contracts and hosted/source briefs.

Ruff and Black pass for both changed/new fleet modules and the three changed
Python test files. The inherited rotate script has 147 Ruff findings at both
base and head, with no new diagnostic code/message; compilation and diff check
pass. A mistyped test filename initially collected no tests; the final command
above uses the real hosted-review test file.

## Per-file change

- `legacy_review_source.py`: bounded verification of existing native evidence.
- `production_review.py`: a verified legacy local source may remain provider
  unknown while requiring a distinct reviewer and honoring provider pins.
- `skfleet-rotate.py`: all three local review capacity checks supply current
  card context; the capacity wrapper verifies legacy evidence before routing.
- `test_legacy_review_source.py`: happy path and refusal cases.
- `test_production_review.py`: unknown-family legacy routing and independence.
- `test_production_dispatch.py`: actual rotate helper and caller coverage.
- `changelog.d/sep21-legacy-review-route.md`: release note.
- This document: reproducible evidence and limitations.

## Remaining work

Independent review of this PR, merge and operator deployment are separate.
For b3416bde, preserve its historical PASS and reconcile the stale lifecycle or
fold remaining work into the surviving W7 review. No uncovered assertion on its
old candidate was established here; do not mint another review or a new PASS.
