### Fixed

- Niobe now releases and retires a remote review generation whose worker exited non-zero with no review packet, so the review is offered again instead of holding its claim forever. Requires exactly one recorded failed exit for that owner and claim; `retire()` re-verifies everything, including unit death.
