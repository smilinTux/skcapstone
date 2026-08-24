# SKCP-00F1 contract remediation evidence

Date: 2026-08-23
Card: `ee1d0874`
Owner: `jarvis`
Status: complete

## Scope

This remediation preserves every original candidate artifact byte exact. The
superseding contract set is under `docs/contracts/v1.1.0/` and is not included
in the original top-level candidate manifest. A new assembly task must pin the
superseding files and obtain a new exact-version human approval.

## Files changed

- `docs/contracts/v1.1.0/control-plane-metric-result.v1.1.0.schema.json`
- `docs/contracts/v1.1.0/control-plane-recommendation.v1.1.0.schema.json`
- `docs/contracts/v1.1.0/control-plane-action-preview.v1.1.0.schema.json`
- `docs/contracts/v1.1.0/control-plane-insight.v1.1.0.schema.json`
- `docs/contracts/v1.1.0/control-plane-report-snapshot.v1.1.0.schema.json`
- `docs/contracts/v1.1.0/openapi.control-plane.v1.1.0.json`
- `docs/contracts/CONTROL-PLANE-CONTRACT-COMPATIBILITY-v1.1.0.md`
- `tests/test_control_plane_contract_remediation.py`

## Acceptance evidence

- Unavailable, unknown, and not-applicable metric results require a null value.
- Numeric zero requires at least one source evidence reference and remains
  valid for an evidence-bearing current or partial result.
- Missing value, failed unavailable value, and partial value are distinct
  fixtures.
- A projection with nonempty source errors cannot use current truth state.
- Proposed action-oriented recommendations require nonempty practice, impact,
  risk, counter-indicator, alternative, and precondition grounding.
- Insufficient evidence uses a typed `abstained` recommendation with a required
  `abstention_reason`.
- Ready high-risk, external, destructive, and protected-Matter previews require
  an approved exact-version approval entry.
- Needs-approval, denied, expired, and approval-bypass fixtures are covered.
- The superseding OpenAPI document is version `1.1.0-superseding` and points to
  the versioned sibling contracts.

## Commands and results

```text
python -m pytest -q tests/test_control_plane_contract_remediation.py
12 passed, 2 warnings

python -m pytest tests/ -q
264 passed, 143 warnings

ruff check tests/
All checks passed!
```

The warnings are the existing jsonschema `RefResolver`, PGPy, and cryptography
deprecation warnings. No test failed.

Additional checks passed:

- Original manifest artifacts: 11 checked, 0 hash mismatches.
- Versioned JSON parse and Draft 2020-12 schema validation.
- Versioned OpenAPI references: 111 checked, 0 missing external files.
- Remediation files contain no em dash or en dash characters.

Superseding SHA-256 values for assembly:

```text
control-plane-action-preview.v1.1.0.schema.json 8cfbabbe47b971f1e2b8ccc4c90ff77a17701caf84d209f0a4fe2a257b47ae21
control-plane-insight.v1.1.0.schema.json 373d9c4067aa0c1fba0f35760ace5aab77583819a6c2471b79792dfaa91a9939
control-plane-metric-result.v1.1.0.schema.json a55d5de1db6241e5067a4d50c96a6bdd861b0b324785c54def7b92782de45c20
control-plane-recommendation.v1.1.0.schema.json d9a80faae276d2ce22a0189c1a03081dd2b293618e88838612df16895dd49fcd
control-plane-report-snapshot.v1.1.0.schema.json be81cf4825f744aa271e91acaee59d49477d19238d9d26a9844e93f9c943af4e
openapi.control-plane.v1.1.0.json fe0deffcbcbde0cad255ba4f0c2905d86068db0832dad02eff1fb32c09ea16cb
```

## Migration and rollback

No runtime, database, deployment, or external integration changed. The
original contract set remains the rollback and audit baseline. A consumer can
return to the original candidate contract files until the superseding set is
approved and adopted by the separate assembly task.

## Non-authorization

This evidence does not authorize production API implementation, deployment,
external actions, protected Matter retrieval, HammerTime Inbox access, or
changes to the original candidate manifest.
