`coord accept` no longer refuses a passing cargo log: zero-count clauses such as `0 failed` are removed before the failure check, so every nonzero or uppercase FAILED line still refuses.
