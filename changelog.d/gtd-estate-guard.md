### Added

- GTD stores carry an optional `coordination/gtd/ESTATE` marker (`nor` or `chi`); a host of another estate refuses to write that store (`EstateMismatchError`), so Chef's and Casey's estates keep two distinct GTD stores. Host estate comes from `SKWORLD_ESTATE`, else the `nor*`/`chi*` hostname prefix.
- `skcapstone gtd capture --source chi-estate|nor-estate` for items another estate's agent sent over skmail.
