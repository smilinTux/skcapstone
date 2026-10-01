# Shared cursor contract follow-up

Card `c768722c`, owner `codex-cursor-contract-c768722c`.
Base `9f92569e99747a9d7ed52c6860e14fde85cfe780`.

Final integration review discovered two repeat-cycle failures after the original
a31 trial was accepted. Its historical review, tests and PASS remain unchanged.
The controller writes a `dispatch` scan hint but the reader allowed only `queue`
and `qualified`. Separately, the scanner can encounter `malformed.json` and write
that basename as its hint, while the reader accepted only eight-hex card names.
In each case one call succeeded and every later call refused the saved cursor.

The parser now accepts the three actual scan keys and bounded JSON basenames
without slash or NUL. Cursor hints only order metadata names. They do not become
lookup paths, qualify cards or authorize replay. Unknown keys, unsafe filenames,
unsafe cursor files and all downstream qualification checks remain refused.
The two controller tests identified by the parent now chmod their fixture JSON
files to 0600, rather than relying on a permissive inherited umask.

Before the production change, two new regression methods failed with
`refill-invalid-cursor`. After the change:

- `python3 -m unittest discover -s ops/fiber-f1be0929/tests -p test_fiber_refill.py`:
  30 passed, zero skips, 1.037 seconds.
- `PYTHONPATH=src SKFLEET_LIVE_SANDBOX_TESTS=1 /home/skuser01/.skenv/bin/python -m pytest -q ops/fiber-f1be0929/tests/test_fiber_refill.py ops/fiber-f1be0929/tests/test_fiber_control.py ops/fiber-f1be0929/tests/test_fiber_selection.py`:
  86 passed, zero skips, 1.60 seconds.
- `git diff --check`: passed.

The shared-state regression uses real refill and queue functions, persists two
running cards, starts a fresh Python reader process, reaches the next eligible
card and verifies prior running requests stay byte-identical and are not replayed.
A malformed non-card filename remains retained and cannot poison later reads.
Traversal, absolute paths, NUL, non-string names and unknown fields stay rejected.

Logs are retained in the private evidence directory for this card. Exact final
candidate hashes, independent actual model review and parent composition results
are recorded separately. No deployment was performed by this leaf. Parent owns
reviewed rollout with backups. Rollback restores the prior reader file only after
accounting for retained dispatch/basename hints; preserve cursor and run evidence.
