### Fixed

- Link lineage now accepts an explained terminal review verdict such as
  `PASS exact-head checks green`, `FAIL|lint` or `BLOCKED: dependency
  unavailable`, and records the typed token (`PASS`, `FAIL`, `BLOCKED`). The
  verdict had to be the bare token, so a done review with an explanation was
  treated as unresolved and Link re-materialised review work on unchanged
  heads. Lookalikes (`PASSED`, `PASSWORD`, `PASS_FOR_REVIEW`, non-strings) still
  fail closed. Ported from #499 (card 5c0a0004).
