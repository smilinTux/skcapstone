# a62b5f8d: atomic recovery of every rejected chiap08 overlay row

## Authority and exact inputs

Card `a62b5f8d`, claim revision
`69453a0bdc094faea5b618df5d47e43a`, authorizes one bounded recovery of all
rejected records enumerated by the supported scanner in
`/home/skuser01/.skcapstone/coordination/card_events/chiap08.jsonl`.

The implementation starts from independently reviewed commits:

- SKCoord `64e8bfa9b668f7727030798e72fde44cbd42bc0a`, tree
  `97cbd11b3074dad6a971e8e581502e124351c9ba`;
- SKCapstone `a4d46e4b4ab299862af47a96559789856f9cb587`, tree
  `1ac3020bfd6758def598f23b5ecc3401ec7a88ac`;
- independent review SHA256
  `8fe3472262efb2f1852e966874c6e2d280c5e7b56c566dbe16b130631b0ce9d9`.

The supported diagnostic artifact
`/home/skuser01/.skcapstone/evidence/work/3c65c0fc/supported-overlay-diagnostics.json`
has SHA256 `c96cffc0d70891d470d275e03a3bf6657db86c0c9c69afffb638cb02866c8e17`.
It binds source generation
`b112e3420390a82165ea21bbd588e3a0994652aed888d42c0d9511186b55b95a`
and exactly these rejected rows:

1. line `10977`, SHA256
   `68c15e4ef388ed88d6a1879cab32a6b0ff76c44d6efc64c43e24c381b14a10eb`,
   card hint `ae44a693`, action hint `verdict`;
2. line `19797`, SHA256
   `521dea6229a40e43d35ec12b9399f6d836ecad9a8c8d440d0b0df1b118c554a6`,
   card hint `086ea05c`, action hint `verdict`;
3. line `28501`, SHA256
   `dac89ec03055b114fe25b3afb58ef277fa320d754afad22e17b08412f3561d68`,
   card hint `e8f3a5b7`, action hint `verdict`.

Valid append-only records may extend that generation before maintenance. The
live plan must bind the stable current shard after quiescence and reproduce
exactly the same three rejected line numbers, hashes, and diagnostics. Any
fourth rejected row or source movement after stable binding stops the task.

## Required behavior

Extend the reviewed single-row planner minimally to one atomic ordered
multi-row plan while preserving single-row compatibility. One sealed plan
binds the source shard SHA256 and a strictly increasing list of targets. Each
target carries its physical line number, exact SHA256, and schema diagnostic.
Planning removes all targets from one immutable image and requires the result
to be strict-valid before writing the plan.

Apply revalidates the source and every target under the stable writer lock,
preserves the original shard, publishes each rejected row as a separate
hash-bound artifact, publishes durable intent, replaces the shard once, fsyncs
custody and replacement state, verifies strict validity, and seals a receipt
that names every target and artifact. Exact retry returns the existing receipt.
Rollback restores the byte-exact original only when the repaired hash matches.

The CLI accepts repeated `--line` and `--line-sha256` pairs. Existing one-row
use remains valid. Planning rejects missing pairs, duplicate or unordered
lines, invalid hashes, changed targets, unsafe evidence, unlisted rejected
records, and any strict-invalid result.

## Test and activation sequence

1. Add a failing synthetic multi-row test reproducing the single-row refusal.
2. Implement only required schema, evidence, apply, retry, rollback, and CLI
   changes.
3. Run focused and broad reviewed suites, Ruff check and format, and diff checks.
4. Commit exact candidates and obtain independent source review of exact
   commits, trees, diffs, tests, evidence separation, retry, and rollback.
5. After PASS, build reviewed wheels and a tested runtime rollback archive.
6. Freeze coordination writes, stop old appenders, and require two identical
   shard observations plus a supported scan returning exactly three targets.
7. Install, plan, apply, then verify doctor, strict folds, separate custody,
   receipt hashes, and idempotent retry before restarting writers.
8. On a new rejected row, movement, or failed postcondition, keep writers
   stopped, use supported rollback if needed, restore packages and units, and
   report the exact blocker.

## Boundaries and evidence

Never append, edit, truncate, replace, or delete event JSONL directly. Do not
interpret rejected records as accepted state, touch unrelated cards, expose
secrets, push source, or deploy elsewhere. Preserve all `bf712d72` and
`3c65c0fc` evidence. Store candidate, review, live preimages, plan, per-row
custody, receipt, rollback proof, strict folds, and unit restoration under
`/home/skuser01/.skcapstone/evidence/work/a62b5f8d/`.
