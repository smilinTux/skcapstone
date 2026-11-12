- `scripts/ci-check.sh` step 5 no longer crashes with `KeyError: 'version'` on this
  setuptools-scm project. New `scripts/check-version-consistency.sh` requires a dynamic
  version, resolves it through setuptools-scm, rejects an empty result, and compares to
  `package.json` only when one exists. Port of #522 onto current main.
