### Added

- `skcapstone mero charter observe` is a read-only report of what the estate is
  actually working on, derived from card title prefixes: per workstream card
  count, provisional delivery fraction (verdict plus evidence on a terminal done
  event), cards claimed in the last 7 days, the latest delivery, and the
  high-energy no-delivery-in-30-days pathology. The alias map lives in
  `data/mero_workstreams.json` as an explicitly DRAFT, unratified file with a
  reason on every entry; card-class tokens (REVIEW, EVIDENCE and the rest) are
  excluded from the workstream namespace; unprefixed cards are counted and
  sampled. It never writes cards or events and is not part of the Link or Mero
  seat cycles. Ported from #347 (card 5c0a0004); the original card is ece35ca2.
