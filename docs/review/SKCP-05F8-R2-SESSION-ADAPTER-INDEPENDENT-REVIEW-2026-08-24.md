# SKCP-05F8-R2 independent session review

Verdict: FAIL

Card `58ed1ac0` independently reviewed exact SKDashboard merge
`b3b49f85ccfdd785550b7918bd131e831686f2a6`, PR 71, and the built v0.1.64
wheel. No author repair, deployment, restart, key action, token action, or live
service change was performed.

## Blocking findings

1. Concurrent protected reads near access-token expiry can both use the same
   one-use refresh credential. The persisted `refreshing` marker is not
   rejected, and the second request can replace the first reservation. The
   exact reproduction made two outbound refresh calls. CapAuth replay handling
   can invalidate the whole family.
2. Logout deletes only the local session row and cookie. It never invokes the
   pinned CapAuth refresh-family revocation endpoint, so a previously copied
   refresh credential remains current after logout.
3. Every unauthenticated `/auth/login` request creates a durable row. There is
   no expired-row cleanup, per-source limit, or global bound. Five hundred
   requests created five hundred retained rows and a 159744-byte database.
4. CapAuth unreachability and encrypted-session corruption both resolve to
   nonretryable `401 UNAUTHORIZED`. This collapses server-side unavailable or
   Unknown state into caller authorization failure.

## Positive controls

- The exact wheel built, passed Twine, installed cleanly, and contained the
  exact reviewed session module.
- Fourteen focused session and read-only runtime tests passed. Ruff passed.
- Opaque Secure HttpOnly SameSite Strict host-only cookies, encrypted storage,
  PKCE, nonce, issuer and audience checks, CSRF, restart, backup recovery,
  PEP reuse, route ceiling, browser nonleakage, and disabled rollback are
  present.
- PR 71 CI was fully green.

## Evidence

- Codex Security report:
  `/tmp/codex-security-scans/skdashboard/b3b49f85_20260824T1904Z/report.md`
- Report SHA256:
  `dca21e39e2898d90420c033eb4c67212916dbab1493e106b99c5aa7188e1c2ca`
- Sealed manifest SHA256:
  `2bc03dfd4a06772013e38ffabf5f1af3c333c79cea96ed2fe86b031d1d206883`
- Findings SHA256:
  `b71b9985a6a2d86206411b038493559467c1580b193ae2d0a658b47fcde24c17`

The review card must remain REVIEW. A separate repair and independent
rereview are required before durable human session activation.
