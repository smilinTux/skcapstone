# f1be0929 launch-safety installation

2026-09-29, chiap08. Owner jarvis. Deployment remains incomplete.

## Installed

- `~/.skcapstone/runtime/llm-orch/fill-slots.sh` is now a compatibility
  entry point for `skfleet-fiber-dispatch.service`. It refuses new launches
  when that service is absent or its lookup fails. The service is currently
  absent. A real invocation returned 78 and reported no workers started.
- `~/.skcapstone/runtime/llm-orch/make-prompt.sh` no longer instructs workers
  to steal claims or continue after refusal. It validates identities and
  requires mediated owner/revision verification before task actions.
- Existing prompt files, queues, branches, workspaces, sessions, and claims
  belonging to other workers were not modified.

## Review and checks

Self-review of both complete candidates and the prompt diff preceded install.
This is not the independent cross-provider canary review required for rollout.
Behavioral testing found and fixed failure to reject a failed systemctl lookup
that also emitted `loaded`.

`python3 ~/.skcapstone/evidence/work/f1be0929/tests/test_launch_safety.py`
returned seven passing tests against the installed files (0.124 seconds).
Checks cover service absence, lookup failure, start failure propagation,
single service entry, removal of destructive shell operations, prompt refusal
rules, identity validation, and byte-exact backup restoration in scratch.
Both candidates passed `bash -n` before installation.

These tests do not prove fleet admission, distributed claims, live workspace
preservation, provider readiness, or the required distributed canary.

## Rollback

Original files are preserved in this card's `backup/` directory. SHA256:

- fill-slots.sh: `3e156ba7c7238233277d1bfa06c0470707679f09c65a6763343ed44c0e39b599`
- make-prompt.sh: `6e99bee5bb90656c875a172e272f58a4bf525f9e76e02eb500d340ec5b5e2e83`

Scratch restoration of both originals passed hash and shell syntax checks.
Do not run the unsafe original launcher as an automatic rollback. Safe runtime
rollback is to stop new dispatch and preserve worker sessions and artifacts.

## Required remaining work

Preserve the user's existing GLM, DeepSeek, Codex, Pi, and Herdr stage pipeline.
Provide one admission authority across those launch paths, not a replacement
for their stage semantics. Herdr reported 15 named working sessions during
inspection; names alone are not proof of actual provider or current card.
No extra worker may be admitted merely because the legacy name filter missed
an existing one. Kimi remains fenced pending entitlement restoration.

Qualify shared capacity and placement, then prove implementation on chiap02,
independent review on chiap03, and isolated tests on chiap04. The separate F6B
crew package still has a failed source gate; this card does not bypass it.
