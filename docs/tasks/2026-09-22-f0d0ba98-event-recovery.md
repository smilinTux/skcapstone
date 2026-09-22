# f0d0ba98: govern malformed overlay recovery and prevention

Bases: SKCapstone work branch `work/f0d0ba98-event-recovery` at commit `a368cc7fdb313080dd73d83f1272d9bd10991e79`; SKCoord work branch `work/f0d0ba98-event-recovery` at reviewed exact-stream recovery commit `e5f3c09bdbe2b529b7cbf2fd00811468d751d414` over `v0.1.81`.

The coordination overlay loader currently drops malformed `coordination/card_events/*.jsonl` rows after a warning, but operators have no supported, provenance-preserving way to remove one rejected row and later restore it. The append path also accepts event payloads without enforcing the complete schema and existing chain under one writer lock. This leaves malformed evidence in the shared fold and makes unsafe direct edits tempting.

Implement one supported recovery command with separate plan, apply, and rollback modes. Planning must identify exactly one rejected overlay row and bind the live shard path, whole-file SHA256, one-based line number, exact line SHA256, original bytes, and parsed diagnostic hints without changing live state. Apply must reject symlinks and multiply linked files, hold the shard lock, revalidate the full-file and exact-line hashes plus schema diagnosis, preserve the original shard and rejected row in durable evidence, atomically replace the shard while removing only that exact row, and emit a hash-bound receipt. Repeated apply must be safely detectable. Rollback must use the receipt, lock and revalidate the repaired shard, restore the exact original bytes atomically, verify the original hash, and record the rollback result. Concurrent changes, wrong hashes, changed diagnoses, and unsafe paths fail closed.

Set `CardEvent` to forbid unknown fields. Validate action, writer identity, action-specific reserved fields, and the existing writer chain while the append lock is held before appending. Extend doctor overlay diagnostics to schema-validate every nonblank row and report the file, one-based line number, diagnostic card hint when available, and rejected count while preserving healthy card folds. The malformed historical `086ea05c` row must remain rejected and its folded card must remain `BLOCKED`; recovery must never reinterpret or translate its truncated `PASS` text.

Add focused tests for exact-byte preservation, retry, concurrent append rejection, rollback, symlink and hardlink rejection, wrong-schema overlay rows, writer and reserved-field strictness, locked chain revalidation, strict per-card reads, doctor diagnostics, and unchanged `086ea05c` semantics. Run the smallest relevant pytest modules, Ruff, formatting checks, and the repository type check for changed Python code.

Allowed SKCapstone paths are this task TDD, the coordination CLI registration, doctor diagnostics, focused CLI and doctor tests, and one changelog fragment. Allowed SKCoord paths are its matching TDD, `src/skcoord/card.py`, the existing recovery module or one focused replacement module, and focused tests. Update `/mnt/cloud/onedrive/projects/DAVE-AI/sklegal/AGENTS.md` only to add `coordination/card_events` and `coordination/archive` to the direct-edit prohibition and explain that degraded cards remain untrusted until supported recovery plus strict fold verification. Do not add a human approval gate.

The integration boundary is one public SKCoord recovery API used by the supported `skcapstone coord` command. SKCapstone must not duplicate overlay parsing, custody, locking, replacement, or rollback logic. Doctor may call the public SKCoord schema diagnostic, but it remains read-only. The SKCapstone candidate is verified against the isolated SKCoord source candidate through `PYTHONPATH`, without installing either package.

Do not inspect or modify live overlay JSONL bytes, execute recovery against live state, use MCP, expose credentials, install, deploy, push, or alter provider state. Rollback is supported only for the exact synthetic or operator-selected shard bound in a valid recovery receipt; this source task does not authorize running it against the live estate. Because pre-upgrade appenders lock the replaceable data inode instead of the new stable sidecar lock, live apply requires all writers on the target host to be upgraded or quiesced. The plan records this technical prerequisite and the supported CLI requires an explicit quiescence assertion. Commit only the isolated source candidates and write one cross-repo hash-bound receipt under `~/.skcapstone/evidence/work/f0d0ba98/` after all scoped checks pass.

Verification commands:

```text
cd /home/skuser01/work/skcoord-f0d0ba98-event-recovery
python -m pytest tests/test_card_event_recovery.py tests/test_overlay_ledger_integrity.py -q
python -m ruff check src/skcoord/card.py src/skcoord/card_event_recovery.py tests/test_card_event_recovery.py tests/test_overlay_ledger_integrity.py
python -m ruff format --check src/skcoord/card.py src/skcoord/card_event_recovery.py tests/test_card_event_recovery.py tests/test_overlay_ledger_integrity.py

cd /home/skuser01/work/skcapstone-f0d0ba98-event-recovery
PYTHONPATH=/home/skuser01/work/skcoord-f0d0ba98-event-recovery/src:src python -m pytest tests/test_coord_event_recovery.py tests/test_doctor.py tests/test_card_events.py -q
PYTHONPATH=/home/skuser01/work/skcoord-f0d0ba98-event-recovery/src:src python -m ruff check src/skcapstone/cli/coord.py src/skcapstone/doctor.py tests/test_coord_event_recovery.py tests/test_doctor.py
PYTHONPATH=/home/skuser01/work/skcoord-f0d0ba98-event-recovery/src:src python -m ruff format --check src/skcapstone/cli/coord.py src/skcapstone/doctor.py tests/test_coord_event_recovery.py tests/test_doctor.py
```
