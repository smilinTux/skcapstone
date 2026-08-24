# SKCP-05F2R canonical bearer independent rereview

## Verdict

PASS for SKCapstone review card `24bb1398` against exact merged SKDashboard
revision `7ec30f5f3e4f1913360e771b26422401e3414ada`.

This review changed no implementation and performed no deployment, restart,
CMDB mutation, Atlas activation, dispatch, or external action.

## Gate and revision binding

- Dependency `df258657` was `done` before review card `24bb1398` was claimed by
  `codex-skcp-05f2r-review`.
- The review ran in a fresh isolated worktree created directly at the exact
  merged revision.
- After a fresh remote fetch, `origin/main` was
  `5f3b6f68abcdab9e1fd7345cb4bf488e1d07a19f` and contained the reviewed
  revision.
- The `_capauth_authorize` function has identical source bytes at the reviewed
  revision and that latest-main comparison. Its SHA-256 is
  `03712b8056c63380ea8c137e8600bc8e98268a548daac8f4c1f7e46ea3cf9803`.
- Later main changes add `Cache-Control: no-store` and scope parsing around the
  protected routes. They do not change canonical bearer decoding or checks.

## Independent legitimate-path qualification

The qualifier used a temporary `GNUPGHOME`, generated one throwaway Ed25519
OpenPGP signing key, wrote only a temporary CapAuth identity, and minted
60-second `skdashboard` audience tokens with `store=False`. No bearer,
signature, fingerprint, or key material was printed or retained.

The wire value used the canonical CapAuth CLI form:

```python
base64.urlsafe_b64encode(export_token(token).encode("utf-8")).decode("ascii")
```

`PYTHONPATH` was explicitly bound to the isolated worktree. Without that
binding, the host editable install resolves the shared checkout and does not
test the pinned revision.

The real CapAuth signature verifier ran three times. A controlled PDP decision
seam recorded the exact three post-signature calls and returned allow so the
route boundary could be observed without changing live policy:

| Route | Required capability | Result | PDP target |
| --- | --- | --- | --- |
| `/api/v1/overview` | `skdashboard.read` | 200 | `/api/v1/overview` |
| `/metrics` | `skdashboard.read` | 200 | `/metrics` |
| `/api/v1/events` | `skdashboard.events.read` | 200 | `/api/v1/events` |

Every PDP call carried subject `human@example.test`, context service
`skdashboard-api`, and the isolated CapAuth home. Metrics remained below 4096
bytes and SSE returned the heartbeat contract.

## Independent denial qualification

All required denial lanes failed closed:

| Lane | Result | Boundary evidence |
| --- | --- | --- |
| Raw exported JSON, extra padding, newline, and garbage | 401 or 403 | No signature or PDP call |
| Invalid signature | 403 | PDP not called |
| Public browser origin | 403 `ORIGIN_DENIED` | No signature or PDP call |
| Wildcard capability | 403 | No signature or PDP call |
| Wrong capability | 403 | No signature or PDP call |
| Expired token | 403 | Real validity check denied before crypto and PDP |
| Bearer larger than 64 KiB | 401 | Authorizer not called |
| Sensitive denied input | 403 | Not present in denial or metrics output |

Response bodies did not contain the test secret, token prefix, temporary home,
or signing fingerprint.

## Repository gates

- `pytest -q tests/test_control_plane_read_api.py`: `12 passed in 1.00s`
- `pytest -q`: `388 passed, 143 warnings in 30.88s`
- `ruff check src tests`: PASS
- `python -m build`: PASS
- `python -m twine check <temporary-dist>/*`: PASS for wheel and source archive
- `git diff --check`: PASS

Local `gitleaks` was unavailable, so the required secret-scan workflow remains
a publication gate. Repository-wide `ruff format --check` is not a configured
CI gate and reports pre-existing formatting drift at the exact merged revision;
this evidence-only review did not rewrite unrelated files.

After the evidence commit was rebased onto latest main
`5f3b6f68abcdab9e1fd7345cb4bf488e1d07a19f`, the same real-signature and eight
denial-lane qualifier passed again. The rebased focused suite reported
`12 passed in 0.91s`, the full suite reported `398 passed, 145 warnings in
30.71s`, Ruff passed, and `git diff --check` passed.

## Conclusion

The canonical `base64url(export_token)` repair at the exact merged revision
accepts only the tested short-TTL, audience-bound, exactly scoped legitimate
routes and preserves the required malformed, invalid, origin, wildcard,
capability, expiry, size, and leakage denials. The independent verdict is PASS,
subject to the evidence PR's required CI and secret-scan gates.
