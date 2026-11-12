# Fleet qualified runtime implementation plan

Goal: staged deploy and rollback consistently install the qualified test tools, and missing tools make the native readiness gate fail.
Architecture: one declared fleet-qualify extra and one shared package-name tuple, reused by fingerprinting and production drift/readiness. Existing dependency and installation boundaries remain intact.
Authorization: source PRs and rebase auto-merge are approved; the operator performs main rollout.

- [x] Add failing extra/install/missing-dependency/drift/readiness tests.
- [x] Implement fleet-qualify extra, shared dependency probe, both install steps and fail-closed production observers.
- [ ] Run affected tests and required checks; publish PR and enable rebase auto-merge.
- [ ] After merged-main rollout, run governed source-specific qualifications and verify GLM dispatch. Recipe extensions are a separate PR.
