### Fixed

- Fleet workers retry any upstream 502 (bounded, inside the request budget) instead of losing the whole session to one gateway blip; oversized-request 502s stay terminal.
- Failed-review recovery moves a released review card back to the review column before retiring it, since release-claim lands cards in backlog.
