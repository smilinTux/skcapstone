### Fixed

- A timed-out gateway route probe no longer reports zero review capacity.
  `acquire_review_route_snapshot` probed the gateway with a hardcoded 8s
  timeout; on failure it writes `routes: []`, and `aggregate_review_capacity`
  turns an empty route list into zero capacity. Every Seraph cycle then
  reported `seraph_no_available_capacity` and the review pipeline stopped
  dispatching while the gateway itself was healthy and mostly idle. The
  timeout now defaults to 20s and is overridable with
  `SKFLEET_ROUTE_PROBE_TIMEOUT`.
