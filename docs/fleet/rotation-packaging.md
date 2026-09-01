# skfleet-rotate Packaging and Deployment

This document describes the versioned packaging approach for `skfleet-rotate.py`, which replaced the previous scp-based deployment method.

**Card:** 41f84c4f - SKFLEET-ROTATE-PACKAGING-01

## Problem Statement

Previously, `skfleet-rotate.py` was deployed by manually copying a file to `~/.local/bin` on five hosts. This approach had several problems:

1. **No version identity:** A hand-copied file has no version identifier, making it impossible to determine which revision a host is running.
2. **Silent drift:** Five hosts could silently hold different revisions, undetectable until an outage occurred.
3. **Manual verification:** Each deployment required manual verification that the file landed, syntax parsed, and fixes were not reverted.
4. **Lost hotfixes:** A guard applied by hand to all hosts was absent from both main and the PR, so merging would have silently reverted the fix.

## Solution

`skfleet-rotate.py` is now shipped as a versioned Python package artifact with:

1. **Package-based installation:** The rotation script is installed as part of the `skcapstone` package via pip.
2. **Version tracking:** The rotation reports its version via `--version` and `--version-info` flags.
3. **Drift detection:** Deployments fail loudly if hosts end up on different revisions.
4. **Hotfix preservation:** Emergency fixes can still be applied quickly.

## Architecture

### Package Structure

```
skcapstone/
├── src/skcapstone/fleet/
│   └── rotation.py          # Rotation logic as importable module
├── scripts/fleet/
│   ├── skfleet-rotate.py    # Original script (preserved for standalone use)
│   └── deploy-rotation.py   # Deployment script with version verification
└── pyproject.toml           # Console entry point: skfleet-rotate
```

### Entry Point

The `pyproject.toml` defines a console script:

```toml
[project.scripts]
skfleet-rotate = "skcapstone.fleet.rotation:cli_main"
```

This installs `skfleet-rotate` as a command in the virtualenv's `bin/` directory.

### Version Reporting

```bash
# Show version
$ skfleet-rotate --version
skfleet-rotate 0.15.97

# Show detailed version info as JSON
$ skfleet-rotate --version-info
{
  "rotation_module_version": "0.15.97",
  "package_version": "0.15.97",
  "file_path": "/home/skuser01/.skenv/lib/python3.12/site-packages/skcapstone/fleet/rotation.py"
}

# Verify against expected version
$ skfleet-rotate --verify 0.15.97
Version matches: 0.15.97

# Version mismatch causes exit code 1
$ skfleet-rotate --verify 0.0.0-fake
VERSION MISMATCH: expected 0.0.0-fake, running 0.15.97. Deploy the correct version.
```

## Deployment

### Normal Deployment

1. Build and push a new skcapstone release
2. Deploy to fleet:

```bash
python scripts/fleet/deploy-rotation.py --version 0.15.97
```

This will:
- Check each host's current version
- Deploy if version doesn't match
- Verify the deployed version
- Report any drift

### Check-Only Mode

Check versions without deploying:

```bash
python scripts/fleet/deploy-rotation.py --version 0.15.97 --check-only
```

### Dry Run

See what would happen without making changes:

```bash
python scripts/fleet/deploy-rotation.py --version 0.15.97 --dry-run
```

### Selective Deployment

Deploy to a subset of hosts:

```bash
python scripts/fleet/deploy-rotation.py --version 0.15.97 --hosts chiap01,chiap02,chiap03
```

## Hotfix Procedure

During an emergency, you can still deploy a fix quickly:

### Option 1: Quick Install from Local Build

```bash
# On the affected host(s)
cd ~/work/skcapstone/repo
git pull
git checkout <hotfix-branch>
pip install -e .

# Verify
skfleet-rotate --verify <expected-version>
```

### Option 2: Direct Script Replacement (Fastest)

For immediate outage mitigation:

```bash
# On the affected host
# 1. Copy the fixed script directly
scp user@source:~/work/skcapstone/repo/scripts/fleet/skfleet-rotate.py \
    ~/.local/bin/skfleet-rotate.py

# 2. Make executable
chmod +x ~/.local/bin/skfleet-rotate.py

# 3. Verify it works
~/.local/bin/skfleet-rotate.py --go
```

After the outage is resolved, follow the normal deployment process to properly package and track the fix.

### Option 3: Emergency Wheel Deployment

```bash
# Build a wheel locally
cd ~/work/skcapstone/repo
pip install build
python -m build

# Deploy to affected host(s)
scp dist/skcapstone-0.15.98-py3-none-any.whl chiap01:~/
ssh chiap01 "pip install --force-reinstall ~/skcapstone-0.15.98-py3-none-any.whl"
```

## Drift Detection

The deployment script detects and reports version drift:

```bash
$ python scripts/fleet/deploy-rotation.py --version 0.15.97

chiap01: uptodate
  Already running 0.15.97

chiap02: mismatch
  Version mismatch: have 0.15.96, need 0.15.97

chiap03: uptodate
  Already running 0.15.97

WARNING: Version drift detected!
  0.15.97: chiap01, chiap03
  0.15.96: chiap02

Summary: 2 up-to-date, 0 deployed, 0 failed, 1 mismatched
```

Drift causes the deployment script to exit with code 1, preventing silent inconsistencies.

## Acceptance Criteria Status

### 1. Versioned Artifact Installation
- [x] Rotation is installed via pip as part of skcapstone package
- [x] `skfleet-rotate --version` reports the version
- [x] Version matches skcapstone package version

### 2. Deploy Fails Loudly on Drift
- [x] `deploy-rotation.py` checks each host's version
- [x] Mismatched versions are reported
- [x] Deployment exits with error code on drift

### 3. Hotfix Capability Preserved
- [x] Three hotfix options documented
- [x] Direct script replacement still works
- [x] Emergency wheel deployment supported

### 4. Verification by Deliberate Drift
To verify drift detection works:

```bash
# Deploy to 4 of 5 hosts (skip chiap05)
python scripts/fleet/deploy-rotation.py --version 0.15.97 \
  --hosts chiap01,chiap02,chiap03,chiap04

# Check all 5 - should report drift on chiap05
python scripts/fleet/deploy-rotation.py --version 0.15.97 --check-only
```

Expected output shows chiap05 as not-installed or mismatched.

## Migration from Old Deployment

### Before

```bash
# Old method - manual scp
scp scripts/fleet/skfleet-rotate.py chiap01:~/.local/bin/
scp scripts/fleet/skfleet-rotate.py chiap02:~/.local/bin/
# ... repeat for each host

# Manual verification
ssh chiap01 "python -m py_compile ~/.local/bin/skfleet-rotate.py"
ssh chiap02 "python -m py_compile ~/.local/bin/skfleet-rotate.py"
# ... repeat for each host
```

### After

```bash
# New method - versioned deployment
python scripts/fleet/deploy-rotation.py --version 0.15.97
```

The script handles all hosts, verification, and drift detection automatically.

## Troubleshooting

### Command Not Found

If `skfleet-rotate` is not found after installation:

```bash
# Activate the correct virtualenv
source ~/.skenv/bin/activate

# Or use the full path
~/.skenv/bin/skfleet-rotate --version
```

### Version Reports Unknown

If the version is "unknown":

```bash
# Reinstall skcapstone
source ~/.skenv/bin/activate
pip install --force-reinstall skcapstone

# Verify
skfleet-rotate --version-info
```

### Deployment Fails on One Host

If deployment fails for a specific host:

```bash
# Deploy to that host only
python scripts/fleet/deploy-rotation.py --version 0.15.97 --hosts chiap02

# Check the host directly
ssh chiap02 "source ~/.skenv/bin/activate && skfleet-rotate --version-info"
```

## Testing

Run the packaging tests:

```bash
pytest tests/test_rotation_package.py -v
```

Tests cover:
- Version reporting
- Version verification
- Package structure
- Console script availability
- Backward compatibility
- Drift detection

## References

- Card: 41f84c4f - SKFLEET-ROTATE-PACKAGING-01
- Module: `src/skcapstone/fleet/rotation.py`
- Deployment script: `scripts/fleet/deploy-rotation.py`
- Tests: `tests/test_rotation_package.py`
