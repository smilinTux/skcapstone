### Fixed

- A review card whose finished generation was superseded (verified by `retired_offers()`) is no longer parked as `awaiting_review` or `terminal_review` by its old PASS, so the fresh review is offered.
