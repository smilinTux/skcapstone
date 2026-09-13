# Casey administrator readback checklist

Run `OWNER=smilinTux ./readback.sh`, then retain the output directory and SHA256SUMS. The card request names `smilinux`; the current credential returns HTTP 404 for every `smilinux/*` repository. This package therefore records the requested-owner failures and the reachable `smilinTux/*` state. Casey must confirm the canonical owner before treating the latter as authoritative.

For each repository and its integration branch:

- [ ] Repository and default/integration branch identity recorded.
- [ ] Branch protection read succeeds; verify required pull request review, conversation resolution, linear history, force-push/delete restrictions.
- [ ] Required status checks are strict/current-commit checks, and required contexts are listed.
- [ ] Rulesets are reviewed for parent rules and bypass actors.
- [ ] Merge queue capability/configuration is recorded. A missing field or GraphQL failure is a gap requiring an administrator UI/API check.
- [ ] Environments list is recorded; each deployment environment has required reviewers, wait timer, branch/tag restrictions, and secrets/variables policy.
- [ ] Deployment history and workflow definitions are retained.
- [ ] Every deploy workflow has concurrency with a stable environment key and cancellation policy that prevents two deployments per environment.
- [ ] Release workflow produces immutable, content-addressed evidence (artifact digest/SHA), and promotion consumes that evidence rather than rebuilding.
- [ ] Any 403/404 is recorded exactly; do not infer PASS from an inaccessible endpoint.

Minimum decision: PASS only when every item is evidenced for all four repositories. Otherwise PASS_FOR_REVIEW for Casey's administrator decision, or BLOCKED if canonical owner/branch identity is not supplied. No endpoint in this package changes GitHub state.
