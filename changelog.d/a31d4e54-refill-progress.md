- Card `a31d4e54`: refill now counts only queued cards WITHOUT an existing
  ledger run against the two-item unlaunched buffer, so two RUNNING or
  LAUNCHING jobs no longer occupy the buffer and block a third eligible card.
  The fatal total-directory-entry ceiling is gone: a large retained or spent
  qualification inbox is metadata, not capacity, and no longer raises
  `refill-too-many-inputs`. Queue and qualification scans decode at most one
  bounded `MAX_INPUTS` (32) lexical page per pass, skip filenames that already
  have a recorded run before decoding, and persist an atomic wrap-around cursor
  in the private state directory so a stable finite inbox eventually reaches
  later eligible work. HOLD gates all reads and writes, dry runs write no
  cursors or jobs, unsafe cursor inputs are refused, and terminal queue
  archival stays byte-preserving and recoverable. Selection no longer counts
  global qualified-inbox depth and adds at most two new pinned qualifications
  per pass. Controller dispatch reads one bounded fair queue page under the
  existing authority lock, skipping recorded runs before JSON decoding, while
  every existing claim, source, qualification, review and downstream dispatch
  gate is preserved.
