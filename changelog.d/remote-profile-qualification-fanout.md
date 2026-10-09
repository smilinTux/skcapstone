### Fixed

- Missing profiles now qualify from fixed, source-bound recipes as governed remote native jobs. Successful receipts publish profiles before dispatch; stale profiles continue to use their stored trusted recipes.
- Initial qualification uses its own immutable plan chain, preserving any earlier unstarted plan for the same source revision.
- An operator-selected recipe is parsed and validated against the fixed native command allowlist; mixed Python and frontend scope gets a composite profile.
