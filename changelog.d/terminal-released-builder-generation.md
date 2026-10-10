### Fixed

- A builder generation whose claim the authority already released (card back in ready or backlog, no owner) now closes as blocked instead of holding `awaiting-evidence` forever, so fresh offers for that card start again.
