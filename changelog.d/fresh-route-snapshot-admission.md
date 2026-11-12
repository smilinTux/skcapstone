### Fixed

- Fleet admission refreshes the gateway route snapshot before each pick, as
  the candidate scan already did. Admission runs minutes after the scan in a
  long cycle, so picks past the snapshot TTL were skipped as
  `route-snapshot-stale` or `no-compatible-healthy-lane:glm` while the gateway
  was healthy: 48 such skips in 12 cycles on 2026-10-09, with ready GLM work
  waiting.
