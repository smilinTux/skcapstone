# SKCP-00F1A V1.1 contract remediation evidence

Card: bd732651
Date: 2026-08-23
Status: complete pending board update

## Scope

Only the V1.1 superseding contract set, its compatibility note, the dedicated
contract remediation tests, and this evidence file changed for this card.
Original manifest-pinned files were not edited.

## Corrections

- Insight and report snapshot schemas self-declare schema_version: 1.1.0.
- The V1.1 OpenAPI document self-declares info.version: 1.1.0.
- Tests bind each V1.1 filename to its schema ID, title, and self-declared
  version, and verify every external local reference remains V1.1.0.
- The compatibility note records the V2 mappings for policy filtering,
  service scope, preview states, changed parameters, proposal, and abstention.

## Changed files

- docs/contracts/v1.1.0/control-plane-insight.v1.1.0.schema.json
- docs/contracts/v1.1.0/control-plane-report-snapshot.v1.1.0.schema.json
- docs/contracts/v1.1.0/openapi.control-plane.v1.1.0.json
- docs/contracts/CONTROL-PLANE-CONTRACT-COMPATIBILITY-v1.1.0.md
- tests/test_control_plane_contract_remediation.py
- docs/evidence/SKCP-00F1A-V1.1-CONTRACT-REMEDIATION-2026-08-23.md

## Verification

- pytest -q tests/test_control_plane_contract_remediation.py: 16 passed.
- pytest -q: 272 passed, 143 warnings.
- ruff check src/ tests/: all checks passed.
- Draft 2020-12 checks: all six V1.1 JSON documents passed
  Draft202012Validator.check_schema.
- Local reference checks: all external references resolve to one of the six
  V1.1 files and contain .v1.1.0.
- Forbidden dash scan over the changed contract, test, and evidence scope:
  passed with no U+2013 or U+2014 characters.

## Original manifest hash preservation

The eleven original candidate files retain their manifest SHA-256 values:

    a1ea6ddc823edf8e7e17bcf800785d0bf4451e33622900f9dbbda362144c6553  docs/architecture/ADR-0001-CONTROL-PLANE-MEASUREMENT-AND-REPORTING.md
    03c77ad31ba890431bf08ef6fb9e2f5e954192a1e5f19b2eac101c1be04dd2f4  docs/planning/SK-CONTROL-PLANE-BREADTH-FIRST-SPRINTS.md
    ffcd4013169b790c9d8ace621987affb55dec99037d0f2ac5e43ef2941af92b4  docs/contracts/control-plane-action-preview.schema.json
    5f37dda3de9a0c60639084924d2b0974421a8d56fc97e9fc2cea0972f4be7587  docs/contracts/control-plane-insight.schema.json
    a4d91290163132d573cb01f5857cf461d3751e29bc51ae27c05f5b14584da30c  docs/contracts/control-plane-metric-result.schema.json
    9864f9f07f4085b82bad06108939ba003d243b0de07cd5e071493df3572fe7d5  docs/contracts/control-plane-recommendation.schema.json
    a05b51dc42ed5f114d615ff16f094bab6dc6f75fbdc3de4f1ee4e2ebee269289  docs/contracts/control-plane-report-snapshot.schema.json
    d23bee601562c2561a2ed998537465d2161c87d410d51ea0183496d51e6050ef  docs/contracts/openapi.control-plane.v1.json
    c073938d90bca705f9e52641715800cbd0c181350849c8ba013729ebf05d4831  docs/wireframes/control-plane-estate-pulse.html
    1fc7d3d1f1150d14fa1295a9c8e0e1d81f4b57440b2bf174115a338ede8e4b93  docs/wireframes/control-plane-estate-pulse.png
    388ec19946df6f2f4e9211119a86a4794b9c5ff47e3c88a59e53f9b9aeb0f616  docs/wireframes/control-plane-authorization-preview.png

## Rollback

No data migration or runtime change occurred. Rollback is limited to removing
this evidence and test addition and restoring the three corrected V1.1
metadata values and the compatibility note from the working tree. The
manifest-pinned files remain the rollback baseline.
