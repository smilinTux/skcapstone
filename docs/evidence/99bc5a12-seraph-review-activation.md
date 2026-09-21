# Seraph private-forge activation candidate

Date: 2026-09-20. Card: `99bc5a12`. Base:
`9b6fbc726fa3632529444e71b6b8d6404516f8d0`.

The owner approved creation of the dedicated Seraph reviewer account and scoped
capability grant. The supplied chiap01 `chef-skgit.env` credential was verified
without printing or copying its token: mode `0600`, authenticated login
`chefboyrdave2.1`, administrator true, and `/admin/users` returned HTTP 200.

## Source controls

- Nonfinite request timestamps are rejected before signature verification or
  nonce recording. The reproduced `nan` freshness bypass now fails.
- The publisher authenticates the fixed Seraph fingerprint
  `FB3C247D1378A222956ACD080F8F9DA4D73DC34E` and maps it only to
  `capauth:seraph@skworld.io`.
- `forge-review:publish` requires VERIFIED enrollment, a signed active grant,
  exact request digest, no legacy unsigned grace, a restart-durable nonce, and
  a durable sanitized audit record.
- Forgejo attestation reads the service login, administrator flag, private
  repository access, and team permissions through PAT-compatible endpoints.
  Owner-only provisioning metadata binds the token hash, ID, name, scope, and
  repository from the one-time administrator response.
- The timer consumes the mediated Link feed and still revalidates live cards,
  artifact bytes, protected main, required checks, ancestry, remote review, and
  immutable receipt at publication time.

## Focused verification

```bash
PYTHONPATH=src /home/skuser01/.skenv/bin/python -m pytest -q \
  tests/test_forgejo.py tests/test_seraph_forgejo.py \
  tests/test_seraph_review_capauth.py tests/test_seraph_review_cycle.py \
  tests/test_seraph_review_publisher.py tests/fleet/test_operator_http.py \
  tests/fleet/test_operator_http_nonfinite.py \
  tests/fleet/test_timer_enablement.py
```

Result before account ceremony: **230 passed in 12.67s**.

## Activation and rollback boundary

Source tests do not authorize installation. After independent source review,
the ceremony creates the restricted forge account/team/token, enrolls Seraph as
VERIFIED, assigns class AGENT, issues the signed bounded grant, installs one
reviewed package and unit revision, and performs one exact-head canary. Remote
approval, CapAuth audit, publisher receipt, and CardStore receipt must agree.

Rollback disables the publisher timer, revokes the Forgejo token and CapAuth
grant, removes team membership, and preserves remote approvals, audits, and
receipts. Account deletion is last and only after history is preserved.
