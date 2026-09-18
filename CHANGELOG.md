# Changelog

## Unreleased

### Changed

- [S] Lane admission cold-start deadlock: an unobserved capacity domain
  (`observed=false`, 0 requests) is no longer fail-closed; it is admitted with
  a capped concurrency (12) so the first card dispatch generates the
  observation that either confirms or condemns the lane.
  (card d7a38a00, option 1)

- [S] Fail-closed is preserved for domains that HAVE been observed in a bad
  state: `unknown` after real traffic, quarantined, owner-down, zero queue
  capacity, and stale snapshots still refuse the lane.

- [S] The 2026-04 staleness/recency gate removed in `9b5c49a1` is NOT
  reintroduced: the new rule keys on `observed` (did the gateway ever serve
  this domain), not on how long ago an observation was taken. An unobserved
  domain cannot have a `lastCheck` to gate on; an observed domain's
  `lastCheck` age is still not a condition (as 9b5c49a1 established).
