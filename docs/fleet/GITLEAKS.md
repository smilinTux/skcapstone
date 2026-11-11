# gitleaks: pinned binary for the filesystem secret sweep

gitleaks (https://github.com/gitleaks/gitleaks) is a pinned skfleet binary, the same
mechanism as worktrunk's `wt` (see `WORKTRUNK.md`). It exists so the fleet-wide secret
sweep runs one known build, not whatever a node happens to have.

## The pin

| Field | Value |
|---|---|
| Version | 8.30.1 (released 2026-03-21) |
| Where | `src/skcapstone/fleet/binaries.py`, `CATALOG["gitleaks"]` |
| Install target | `~/.local/bin/gitleaks` |
| x86_64 asset | `gitleaks_8.30.1_linux_x64.tar.gz`, sha256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` |
| aarch64 asset | `gitleaks_8.30.1_linux_arm64.tar.gz`, sha256 `e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080` |

Upstream names assets `x64` and `arm64`, so the catalog entry carries an `asset_arch`
map from `platform.machine()` to those words.

The hashes were taken from the release's `gitleaks_8.30.1_checksums.txt` and re-checked
by downloading both archives and running `sha256sum`. The backend verifies the hash in
code, never the release's own checksum file at install time.

## Where it is required

Required (and allowed) on the **control** and **builder-standby** profiles. Not required
and not allowed on worker-gpu or observer. A node reads as missing the binary when it is
absent or at another version.

```bash
skfleet install --check                      # drift report: missing_required_binaries
skfleet install --apply --only gitleaks      # download, verify, atomic replace
```

A rollout's `converge_profile` step installs it on opted-in nodes (see `WORKTRUNK.md`).
There is no config file and no post-install step.

## Bumping

Pick a 8.x release at least two weeks old, fetch `gitleaks_<v>_checksums.txt`, download
the `linux_x64` and `linux_arm64` archives and confirm `sha256sum` matches, then change
`version`, the URL and both hashes together in one reviewed commit. Bumping the pin is the
upgrade path.
