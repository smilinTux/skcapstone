### Added
- `tests/test_skip_ledger.py`: validates that every skip-ledger entry resolves to
  a test that actually exists, by AST rather than by grep. An entry that matches
  nothing reads as a declaration while covering no test, which is the defect the
  ledger exists to prevent, one level up. Four of the first seventeen entries
  were mis-keyed and all four would have read as "declared".
