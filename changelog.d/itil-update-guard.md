### Fixed

- `itil change update` and `itil problem update` no longer report a refused
  transition as success. Both printed a green `Updated: <id> -> <status>` line
  even when the transition was folded away and the status never moved, which
  reads as success; the incident path already guarded this. For changes the
  message names the actual refusal reason (illegal transition, the CAB guard,
  or a required note) rather than assuming an illegal transition, because
  `reviewing -> approved` is legal and is refused by the CAB guard instead.
