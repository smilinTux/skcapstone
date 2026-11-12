### Fixed

- Fleet cycles now harvest valid completed native test-profile receipts before
  claiming builders and log the exact reason a profile refresh cannot advance.
- Cycles also recover sealed completed refresh plans when the native receipt
  exists but the queue's job handoff was lost before profile publication.
