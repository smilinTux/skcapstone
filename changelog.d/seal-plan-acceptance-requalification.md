### Fixed

- Sealing an acceptance test plan accepts a profile that `seal_candidate` proved requalifiable (fingerprints only), records `acceptance_requalification` on the plan, and plan re-validation skips only the fingerprint equalities for such plans; completes the #1100 acceptance requalification path.
