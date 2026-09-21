# Seraph approval publication for private SKGit

Card: `99bc5a11`. This source supports the exact SKLegal repository at
`https://skgit.skstack01.douno.it/smilinTux/sklegal`.

## Runtime state

The source publisher includes a bounded timer entrypoint. Installation and live
qualification remain a separate card step. An independent card PASS is not a
forge approval until remote readback and the durable receipt agree.

Link consumes the mediated observation feed. Seraph owns independent review.
The approval connector uses a distinct forge service identity with access only
to the authorized repository. Link never receives that credential.

## Required bindings

The existing `publish_seraph_pass` function requires these trusted ports:

1. A `CapAuthVerifier` that authenticates `capauth:seraph@skworld.io`, verifies
   the exact `forge-review:publish` capability, and binds it to the publisher's
   request digest. A caller-supplied identity or boolean is insufficient.
2. A `ForgejoReviewConnector` with a credential attestor that verifies the live
   service identity, exact repository scope, and `write:repository` permission.
   A successful read or environment variable does not prove credential scope.
3. `LiveCardStoreGateway`, an approved evidence root, and a durable receipt
   directory owned by the publication service.

`SeraphCapAuthVerifier` binds the approved Seraph fingerprint to the fixed
principal, rejects unsigned-grace operation, requires a signed VERIFIED grant,
uses a restart-durable nonce store, and fsyncs a sanitized authorization audit
before returning an allow. `attest_private_credential` reads the live Forgejo
identity, private repository access, and team unit permissions. It also checks
the PAT against owner-only provisioning metadata captured from the one-time
administrator response. Forgejo PATs cannot read token-management endpoints.

The dedicated forge identity is `seraph-review-bot`. It belongs only to team
`sklegal-seraph-reviewers`, which has code read and pull-request write on only
`smilinTux/sklegal`. The token name is `sklegal-seraph-review-publisher`, with
scope `write:repository` restricted to that repository. Forgejo has no narrower
approval-only token scope, so the transport endpoint allowlist and exact payload
remain required.

## Local evidence preflight

Build a JSON request using the fields of `SeraphPassEvidence`: `repository`,
`number`, `head_sha`, `source_card`, `source_card_revision`, `review_card`,
`review_card_revision`, `reviewer_identity`, `evidence_sha256`, and `verdict`
(exactly `"PASS"`). Use current
folded revisions and the independent review artifact hash. Do not put a token
or CapAuth presentation in that file.

```bash
python -m skcapstone.seraph_forgejo preflight \
  --request /path/to/review-request.json \
  --home "$HOME/.skcapstone" \
  --evidence-root "$HOME/.skcapstone/evidence/work"
```

Successful local validation reports `"local_evidence": "verified"` together with
the missing authorization bindings and exits nonzero. It performs no forge write.
Substantive card mutations change the folded revision, so rebuild the request
from current evidence immediately before authorized publication. The publisher's
own receipt links are excluded from that revision to keep retries stable.

## Publication checks

- Source and completed independent review agree on repository, PR, commit,
  candidate artifact hash, reviewer attribution, and current revisions.
- Review artifact bytes match both the card and request, without following
  symlinks. Unresolved sibling reviews block publication.
- The PR is open, its base equals protected main, and bounded immutable Git
  parent traversal proves main is an ancestor. More than 32 visited commits
  or an exhausted time budget fails closed.
- Required checks are currently green. Live protection requires approval,
  rejects outdated branches and rejected reviews, dismisses stale approvals,
  applies to administrators, and disables direct pushes.
- An unresolved rejection blocks publication. Readback requires an official,
  current, non-dismissed approval at the exact commit by the service identity.
- Retries reconcile the remote review with the durable receipt. A different
  review ID cannot satisfy an existing receipt. Receipt links use `skcapstone
  coord`, preserving the canonical verdict.

## Activation and rollback

After the source PR passes protected-main review, qualify the real authorization
and credential bindings on the intended host. First run private observations,
then publish one authorized approval and verify its exact remote review ID,
commit, service identity, and durable receipt. Only then enable automation.
Approval of an input repair does not approve a later combined candidate.

The timer runs `skcapstone.seraph_review_runner` five minutes after boot and
every five minutes thereafter. It consumes only the fresh, hash-bound Link feed,
reconstructs exact evidence from live CardStore folds, signs the exact publisher
request digest with the Seraph key, and attempts only private SKLegal records
whose lineage outcome and CI are PASS. Its Forgejo token and provisioning
metadata stay in `~/api-keys/seraph-skgit.env`, mode `0600`. The file binds the
token hash, numeric token ID, name, scope, and repository returned during the
administrator ceremony. Link never reads it.

Rollback restores the previous executable and service configuration, disables
the new publication job, and preserves all remote review and receipt history.
No protection changes, application deployment, or corpus processing are needed.
