### Fixed

- The Link seat no longer consumes the same observation feed twice. A feed whose
  evidence hash matches the one accepted on the previous cycle is refused with
  `observation_feed_replayed` and the cycle falls back to review-work-only, so an
  unchanged feed cannot append duplicate handoffs. The last accepted hash is kept
  in `coordination/seat-cycles/link.last-feed-sha256` and is only advanced after
  a feed is accepted. Ported from #561 (card 5c0a0004).
