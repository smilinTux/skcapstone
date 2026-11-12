### Fixed
- The skip-ledger hook now records a skip from any phase, not just `setup`. CI
  proved it blind to `pytest.importorskip` called inside a test body, which
  reports at the `call` phase. A mid-test skip is the most dangerous kind: it
  can fire after some assertions have passed and before the rest ever run, so a
  partial pass is reported as a clean skip.
