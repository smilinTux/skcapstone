### Changed

- Python 3.12 is now the floor (`requires-python >=3.12`). Every fleet host runs 3.12 or newer and none runs 3.11, so the 3.11 CI lane (`unit tests (py3.11)`, `scripts/ci/run-python311-compat.sh`) is removed, the push/release matrix is `["3.12"]`, preflight requires 3.12, and the coord review gate no longer requires a `ci_check_python311` link (five CI links instead of six).
