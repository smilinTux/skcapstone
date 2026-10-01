# Exact retained-claim production retry

Card `7ad0ea5b`, parent `9b230773`. Source base
`ccd4eb25ee24c4f172cffa37578687118152bd4e`.

The production trial stopped after an infrastructure failure, with no source
changes or producer proposal. Its exact claim correctly remained held in
awaiting-evidence, but no native operation could authorize another session.
Existing offer is idempotent, stale recovery handles running only, and releasing
the claim alone cannot unblock the stopped status. No producer verdict is
fabricated to work around that contract.

`fleet builder-retry` defaults to read-only qualification. Explicit `--apply`
requires the frozen production authority and a frozen execution-node proof via
existing trusted SSH. The command archives the old request/status, exact proof
and operator failure evidence hash, then renews the existing request lease with
a one-use authorization. The exact claim and request ID remain unchanged.
This implements the root-approved clarification appended through native coord
edit: the original create-time phrase "fresh request/session" means the final
required behavior is same request generation, exact retained claim, renewed
lease, one-use authorization and fresh Pi/unit attempt. Installed coord edit
does not offer acceptance-criterion editing; no raw card edits were made.

The ordinary consumer rechecks the source-only card, exact owner/claim,
request/unit/invocation, stopped process, clean original HEAD and absence of
typed outcomes or candidate artifacts. Trusted Git inspection shares the
existing read-only networkless bwrap boundary. Current gateway/card/resource
qualification still runs. Before launch, native card locking fences custody,
and an immutable consumption receipt plus next-attempt unknown status are
persisted. A crash or launcher error cannot spend the authorization twice.
Pi starts normally without resume/continue flags. Existing MAX_ATTEMPTS remains
the limit. No new claim, release, producer verdict, completion or scheduler is
introduced. Refusal before launch preserves old custody; a newly arriving
candidate follows normal publication and review instead of retry.

Files changed: builder_retry helper, native fleet CLI, narrow builder consumer
seams, shared sandbox inspection helper, focused tests and changelog/evidence.

Validation: the six-file boundary suite covers retry, native builder dispatch,
production builder, source bundle, source transport and production custody.
Actual final result: 164 passed, zero skipped, 8.12 seconds. The exact
candidate descriptor binds the external TESTS.log hash. Tests include a real bwrap/Git
unchanged-base check, exact-claim fresh unit launch once, expiry/policy/source
refusal, candidate arrival, freeze races, CLI check default and launcher-error
replay. Ruff and git diff --check passed.

No runtime installation, actual retry, worker/model request, source push or
trial-card mutation occurred. Independent review and parent installation are
pending. Rollback is the prior source commit; retain all old request, claim,
session, workspace and immutable authorization/consumption evidence. If a
process launch has uncertain results after consumption, custody remains held
for explicit recovery instead of automatic replay.
