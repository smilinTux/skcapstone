# Private SKGit review implementation plan

**Card:** `99bc5a11`, claimed by Jarvis.
**Base:** `fd7e46f2b72882bbfb07df91b08cc5fb60263e1e`.
**Goal:** Observe SKLegal on its private forge and publish independently reviewed
Seraph approvals against the exact reviewed commit.
**Architecture:** Keep the mediated Link feed and existing review contracts.
Add the one authorized Forgejo repository, reuse the historical Seraph publisher,
and keep approval credentials and decisions outside Link. An unavailable trusted
identity, capability, current review, check, or protection record blocks publication.
**Tech stack:** Python standard library, existing CardStore and CapAuth, pytest.
**Spec:** User-directed extension of `docs/fleet/link-observation-producer.md` and
the historical publisher at `e85dd83380ec1bf6561185ed251312a4f73b7c3e`.

## Constraints

- Preserve the four existing GitHub repositories and their identities.
- SKGit identities include the exact forge origin; bare PR numbers cannot bind
  a review across repositories or forges.
- Do not give Link approval authority or use producer credentials to approve.
- No token in arguments, URLs, evidence, exceptions, or committed files.
- No redirects with credentials. No new dependency or account.
- CardStore writes use mediated coordination commands.
- Source qualification and installed runtime activation are separate evidence.

## Work partitions

1. **Transport, parent:** `src/skcapstone/forgejo.py` and
   `tests/test_forgejo.py`. Fixed authorized origin/repository, bounded HTTP,
   pagination, exact-head checks, secret-safe errors, normalized observations.
2. **Observation worker:** existing Link producer, lineage and wrapper modules,
   both service templates, their tests, and producer documentation. Use shared
   connector; require explicit scope; reject cross-forge and stale-head lineage.
3. **Publisher worker:** recover Seraph contracts, publisher, CardStore adapter
   and tests. Add Forgejo adapter and narrow `PUBLISH_REVIEW` permission. Bind
   live review card to repository, PR, head and both identities, beyond the
   historical artifact-hash/revision checks.

## Verification sequence

- [x] Reproduce unsupported private repository, unsafe cross-forge binding and
  unavailable publication in focused failing tests.
- [x] Implement each partition and run its focused tests.
- [x] Run combined Link feed/lineage, publisher and seat-boundary tests.
- [x] Exercise wrong identity, wrong head, changed artifact, stale card, denied
  authorization, failed/missing required check, rejected review, pagination,
  timeout, retry and readback controls.
- [ ] Run repository formatting/lint/docs checks, record exact failures, inspect
  secret delta and publish a source PR for independent review.
- [ ] Activate only an accepted candidate with existing configured independent
  identity/capability. First read-only private observation, then one explicitly
  authorized exact-head review with remote readback. Missing prerequisites stay
  unavailable; do not call source tests a live repair.

## Rollback

Revert the bounded source PR. Restore prior service/config bytes if activation
occurs. Preserve published review and CardStore history; never erase a receipt.
No main protection change, model activation or application deployment is included.

## API verification

Forgejo documents its instance OpenAPI at `/swagger.v1.json`:
<https://forgejo.org/docs/latest/user/api/usage/>. The SKGit instance schema was
read on 2026-09-20 and confirms review creation uses `commit_id`, `event`, and
`body`, and readback includes `commit_id`, `state`, `user`, `dismissed`, and
`stale`. The implementation must verify these fields rather than assume a
successful HTTP write is an independent approval.
