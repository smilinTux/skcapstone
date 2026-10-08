### Fixed

- Remote review consumers now log `POLICY_DRIFT_HELD` and preserve an offer
  when its embedded production policy differs from the current policy. The
  authority can then retire and reoffer that generation through the governed
  lifecycle instead of leaving it stranded without a clear reason.
