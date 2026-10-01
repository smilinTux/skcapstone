# Historical PR association repair

Parent `9b230773` authorized local source implementation and commit only. Baseline `a5a8bafc79046e0f18b1ec4b7a819bfdfb612e28` matches the installed reader, guarded link, completion guard and coordination CLI.

1. Test exact raw fold, full history, owner, claim, PR and repository guards.
2. Retain a structured lineage link before clearing only the mismatched active PR link, using existing native events, lock and event projection.
3. Verify replay, interruption, stale state and unchanged outcomes, then commit for independent review.

No installation, live repair, deployment, source push or physical-device acceptance is authorized in this leaf. Ordinary snapshot validation remains unchanged. Existing history is append-only. Rollback before installation is discarding this isolated candidate; a live repair would be an additive reconciliation and must never erase old events.
