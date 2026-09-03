# Worker Exit Evidence f8ba9db8

## Scope

The shared fleet launch wrapper now preserves terminal child status and bounded,
redacted stderr when a worker exits without stdout. Known pre-agent transport
failures use the scheduler's bounded retry hold and do not consume substantive
attempt budget. Claim revision fencing and live quiet-worker handling are
unchanged.

## Verification

- Focused tests: `49 passed in 1.06s`.
- Full scheduler tests: `226 passed in 5.19s`.
- Static checks: Black check passed for new Python files, Ruff passed for new
  Python files, `py_compile` passed for both fleet scripts, and `git diff
  --check` passed.
- No live worker, deployment, service, or runtime installation was changed.

## Acceptance evidence

- Each zero-stdout exit writes a mode `0600`, create-once JSON record containing
  card, owner, claim revision, host, lane, model, child exit code, attempt time,
  bounded redacted stderr, stdout log identity, and transport classification.
- The transport allow list covers HTTP 429, `model_owner_backend_down`,
  `backend-claims-quarantined`, `invalid_upstream_tool_calls`, and connection
  failures.
- Transport evidence is held for the existing configurable 60-second bounded
  recovery interval and excluded from substantive launch counts.
- Evidence is produced only after child termination. A quiet or card-complete
  worker whose Pi process remains alive continues to count as live.

## Rollback

Revert the implementation commit. This removes the wrapper routing and retry
reader. Existing immutable exit records may remain as inert audit evidence, or
an operator may archive the `~/.skcapstone/evidence/fleet-worker-exits`
directory after retention review. No data migration is required.
