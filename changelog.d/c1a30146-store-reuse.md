### Fixed

- Reuse the fresh CardStore within each locked native acceptance inspection,
  removing its duplicate legacy overlay load while retaining fresh stores
  between inspections and all sibling and replacement predecessor checks.
