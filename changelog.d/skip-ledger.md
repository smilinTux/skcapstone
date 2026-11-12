### Added
- `tests/skip_ledger.txt` plus enforcement in `tests/conftest.py`: a skipped test
  must be declared with a disposition, or the run fails. A skip reads as coverage
  in every summary while being structurally unable to fail the build. This suite
  held 72 tests that can only ever skip on a runner, including
  `test_pi_denies_a_direct_mcp_tool_and_measures_schema_bytes`, which has been
  FAILING on the only machine able to execute it, invisibly, because CI always
  skipped it. Module-level skips (35 tests in `test_cli_completions.py`) are
  caught too; those produce no test report at all and are the most invisible kind.

### Changed
- `pyproject.toml`: pytest `addopts` gains `-rs`, so every skip prints its reason.
  Several comments in `tests/` claimed "`pytest -rs` lists them, so this cannot
  quietly stay skipped" while no invocation anywhere passed `-rs`.
