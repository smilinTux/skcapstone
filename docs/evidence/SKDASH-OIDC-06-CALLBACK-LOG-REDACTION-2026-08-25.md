# SKDASH-OIDC-06 callback log redaction evidence

Card: `014afd11`

## Change boundary

The dedicated read-only launcher adds a filter only to Uvicorn's access-log
handler. For the exact `/auth/callback` path, the filter removes the complete
query string before formatting. It retains the request method, callback path,
HTTP version, and response status. All unrelated access records remain
unchanged.

Application callback diagnostics continue to expose only the existing safe
correlation reference and allowlisted failure category. TLS and callback
validation are unchanged.

## Sensitivity and checks

`test_callback_access_log_filter_is_sensitive_and_callback_only` first proves
that an unfiltered synthetic Uvicorn record contains the injected callback
sentinel. It then installs the filter and proves that synthetic code, state,
nonce, PKCE, and token values are absent. The same capture proves that an
unrelated 503 path and query remain visible.

`test_callback_returns_reference_and_logs_only_safe_denial` separately proves
that application logs retain the safe reference, failure category, status, and
allowlisted detail without the callback state, code, client secret, or token.

Source qualification on 2026-08-25:

- focused session and runtime tests: 25 passed
- complete test suite: 527 passed
- Ruff check and format checks: passed

## Runtime qualification and rollback

Qualification installs only the exact merged wheel into the existing approved
read-only environment and restarts only
`skdashboard-read-only@10.0.0.139.service`. A public-synthetic callback request
must return an honest failure while a bounded journal check reports the path
and status present and every sentinel absent. The check must never print raw
callback records.

If qualification fails, reinstall the cached prior wheel
`skdashboard-0.1.77-py3-none-any.whl`, SHA-256
`66466fff5ab9c7fd62fd26bf6008e89e11de648d1c4f7d196c5d42be775ef028`,
then restart only the same dashboard unit. No schema or data rollback is
required because this change writes no application state.
