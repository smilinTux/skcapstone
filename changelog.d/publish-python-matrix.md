### Fixed

- Publishing is possible again. The push test matrix ran 3.10, 3.11, 3.12,
  3.13 and 3.14 while a pull request only had to pass 3.11 and 3.12, so main
  could be green on every PR and still fail every publish run. Measured
  2026-09-21: 3.10 cannot install (`skwhisper` declares Requires-Python
  >=3.11) and 3.13/3.14 fail on `pgpy`'s import of `imghdr`, removed from the
  stdlib in 3.13.

### Changed

- `requires-python` raised from `>=3.10` to `>=3.11`, and the 3.10, 3.13 and
  3.14 classifiers dropped, so the package metadata stops claiming support it
  cannot deliver.
