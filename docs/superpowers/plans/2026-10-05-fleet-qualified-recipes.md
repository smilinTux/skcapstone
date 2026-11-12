# Qualified Python recipe extension

Card: ad4c4217. The approved scope extends the existing native profile and test
executor. Operator rollout and governed repository qualifications remain
separate from source publication.

- [x] Write failing regression tests for nested roots, directory coverage,
  configurable caps, path safety, and exact known-failure exclusions.
- [x] Share fixed recipe parsing and sealed-source validation across plan
  sealing, execution, and receipt verification.
- [x] Add a fingerprinted read-only pytest collection hook and hash-bound
  selection evidence, with real pytest fixtures proving exact exclusions,
  missing-baseline refusal, and remaining-failure refusal.
- [x] Verify legacy profiles, successor generations, and Node contracts retain
  their existing behavior; document the new optional recipe fields.
- [ ] Publish a PR off main with rebase auto-merge and green required CI.
- [ ] After operator main rollout, qualify the approved repository recipes,
  publish card-bound profiles, and observe the native dispatch result.
