# Canonical cycle receipt and admission order

Card 508dc7d8, source-only candidate.

The real Niobe invocation 07219eb9f8584dd2a5d752335de2ea6f returned dispatcher exit 0 but wrapper exit 70. Its terminal receipt exists under `fleet-rotation/chiap08-07219eb9f8584dd2a5d752335de2ea6f/actions.log`; the wrapper searched the unprefixed directory. The same log records a gateway route probe for unsupported card a8300e02 before its test-profile refusal.

The wrapper now uses the existing dispatcher host and invocation path contract. The profile gate moves ahead of route completion probes and recommendation writes, retaining its cooldown and the separate fresh preclaim recheck. No quota, claim, review, receipt content or acceptance rule changes.

Validation: 31 affected tests passed, zero skips, in 4.05 seconds: `tests/test_niobe_live_entrypoint.py`, `tests/fleet/test_production_test_profile.py`, and `tests/fleet/test_production_cycle_seams.py`. The two regressions first failed on the baseline with wrapper exit 70 and a probe before contract refusal. An initial AST fixture unpacking error was corrected before the meaningful ordering failure. Pytest emitted the known disabled-plugin `asyncio_mode` warning. Ruff, Black and diff checks passed after formatting only.

Independent review, exact installation backups and real canonical recovery remain operator evidence outside this source commit. Rollback restores only the prior dispatcher and Niobe module bytes. No data migration, push or application deployment.
