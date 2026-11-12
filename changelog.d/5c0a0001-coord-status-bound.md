- `coord status --format json [--limit N] [--cursor C]` prints one machine-readable
  document on stdout: summary, an id-ordered bounded page of cards, `has_more` and an
  opaque scope- and limit-bound `next_cursor`. A tampered, stale or wrong-scope cursor
  fails closed. Port of #320 (card d55c6dd3); the MCP `coord_status` already bounds
  its payload on main.
