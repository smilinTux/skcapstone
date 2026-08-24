# ADR-0001 candidate capture amendment 1.1.3

Status: proposed for exact human review

## Decision

This append-only amendment corrects one narrative error in V1.1.2. It does not
modify or replace any V1.1.2 byte. The two historical PNG wireframes are
present as retained lineage artifacts at these paths:

- `docs/review/lineage/v1.1.0/docs/wireframes/control-plane-estate-pulse-v2.png`
  with SHA256
  `33c400d4d4546e120a2662d5ef887d27ee85e4b87f5bdd973e038114d5e8c129`.
- `docs/review/lineage/v1.1.0/docs/wireframes/control-plane-authorization-preview-v2.png`
  with SHA256
  `f1ddf830f41a052917aeab6640183f649c0c8937cf7c441c5f2d1ef3d87463a8`.

Those bytes were recovered and retained during V1.1.2 assembly. They are
historical lineage inputs, not active product visuals. The active visual is
`docs/wireframes/control-plane-estate-pulse-v2.1.html`, and the active schedule
truth supplement is
`docs/review/SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.2.md`.

## Compatibility

The active V1.1.0 contract files remain unchanged. Source truth remains
separate from policy visibility and authorization. Missing, failed,
unreachable, unavailable, unknown, unauthorized, and policy-filtered evidence
cannot become zero, healthy, or genuinely not applicable.

The V1.1.3 manifest pins this amendment, the V1.1.2 manifest and receipt, the
active contracts, the F9 schedule truth repair, and the independent FAIL
record that required this correction. Its detached receipt remains outside the
manifest artifact list.

## Gates

V1.1.3 remains proposed until human gate `9ad1eeb8` approves its exact manifest
hash. Independent rereview `39085b32` remains incomplete after that approval
and must publish its own exact PASS or FAIL result.

## Non-authorizations

This amendment does not authorize implementation, deployment, activation,
restart, external action, protected Matter access, HammerTime Inbox access,
board reconciliation, gate bypass, or completion of any review card.

Rollback is declining V1.1.3 and retaining all prior immutable bytes.
