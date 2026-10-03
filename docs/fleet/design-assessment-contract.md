# Design assessment 888b4895: native test contract

Card 2f622908 qualifies a narrow artifact check through the existing pytest
profile. It does not implement the OpenRig proposal or add a test engine.
The assessed implementation remains commit
`51a3f5dd3093c8e4a388760eed7ca223f6e22f67`; the successor source adds only this
contract and its tests. The proposal and archive retain their original card
digests. Their actual bytes were verified by the operator before qualification.

Plan: add bounded report checks, exercise valid synthetic and denial fixtures,
independently review the exact source, qualify the explicit sandbox recipe,
then refresh only the unowned assessment's source ancestry and native profile.
Niobe owns assignment. Existing node quotas and reservations remain mandatory.

The assessor creates `docs/assessments/888b4895.json`. Its exact schema and
constants are in `tests/design_assessment_contract.py`. Required content:

- Exact schema, card, assessed revision, proposal and archive hashes.
- Recommendation `accept`, `revise` or `defer`, with a summary.
- Findings distinguished as `existing`, `gap` or `assumption`. Sourced findings
  include exact approved source paths, hashes and line spans.
- Deployed observations distinguished as `observed` with a private receipt hash
  or `unverified` with an explicit limitation and null receipt hash.
- A minimal change list, measurable pilot acceptance criteria and limitations.

The native recipe explicitly invokes `tests/design_assessment_contract.py` and
requires its one test to pass. Its filename deliberately avoids ordinary test
discovery while the real report is unfinished. The source card's separate
`tests/test_design_assessment_contract.py` tests qualify the validator itself.
No placeholder report is committed or presented as actual assessment evidence.

These machine checks prove artifact shape and the approved source byte/line
bindings. They do not prove that the prose follows from the citation, that a
private deployed receipt is authentic, or that a recommendation is sound.
Independent substantive review must verify those facts, the original proposal
bytes, and that the assessor changed only the report, leaving this trusted
validator and the assessed implementation unchanged. Design-only authorization
does not permit feature implementation, merge, deployment or external messages.
