### Fixed

- A refused claim now reports why. The detail was `stderr or stdout`, and the
  CardStore fold writes a benign warning to stderr for a malformed historical
  event, so stderr always won and the real error, which the claim CLI writes to
  stdout, was never shown. chiap04 reported `card_events ... is not a card
  event` on every refusal for hours while the actual reason was
  `human claim denied: blocked_on_human=approval:exact-repository-head`.
