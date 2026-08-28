# Rollback Plan: skfleet-rotate.py Source Reconstruction

**Card:** ff68bade
**Title:** [FLEET-ROTATION-SOURCE-RECON-01][M][REPAIR] Reconstruct governed canonical source for installed rotation launcher
**SHA-256:** 36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e

## Overview

This rollback plan documents how to restore the rotation launcher to its current working state if the source reconstruction or any future changes cause issues. The installed launcher at `~/.local/bin/skfleet-rotate.py` is the source of truth for fleet operation.

## Current State

### Installed Launcher
- **Path:** `~/.local/bin/skfleet-rotate.py`
- **SHA-256:** `36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e`
- **Permissions:** `0755` (executable)
- **Last verified:** 2026-08-28

### Source Repository
- **Repository:** `smilinTux/skcapstone`
- **Branch:** `main`
- **Commit:** `06dfa7e`
- **Path:** `scripts/fleet/skfleet-rotate.py`
- **Status:** Matches installed bytes exactly

## Rollback Procedures

### 1. Immediate Rollback: Restore from Installed Copy

If the source repository version causes issues, restore from the verified installed copy:

```bash
# Backup current source (if different)
cp ~/work/skcapstone/scripts/fleet/skfleet-rotate.py \
   ~/work/skcapstone/scripts/fleet/skfleet-rotate.py.backup

# Copy verified installed version to source
cp ~/.local/bin/skfleet-rotate.py \
   ~/work/skcapstone/scripts/fleet/skfleet-rotate.py

# Verify hash matches expected
sha256sum ~/work/skcapstone/scripts/fleet/skfleet-rotate.py
# Expected: 36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e
```

### 2. Git Rollback: Revert to Known Good Commit

If a bad commit was merged to the repository:

```bash
cd ~/work/skcapstone

# Reset to known good commit
git reset --hard 06dfa7e

# Or revert specific bad commit while preserving history
git revert <bad-commit-sha>

# Push the rollback
git push origin feat/ff68bade-reconstruct-rotation-source --force-with-lease
```

### 3. Restore from Evidence Bundle

If both repository and installed copies are compromised, restore from the evidence bundle:

```bash
# Evidence bundle location
EVIDENCE_DIR=~/.skcapstone/evidence/work/ff68bade

# Verify evidence bundle integrity
cd "$EVIDENCE_DIR"
sha256sum -c sha256sums.txt

# Restore launcher from evidence
cp skfleet-rotate.py ~/.local/bin/skfleet-rotate.py
cp skfleet-rotate.py ~/work/skcapstone/scripts/fleet/skfleet-rotate.py

# Verify restoration
sha256sum ~/.local/bin/skfleet-rotate.py
# Expected: 36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e
```

## Verification After Rollback

Always verify the launcher after any rollback:

```bash
# Run the test suite
cd ~/work/skcapstone
python3 tests/test_rotation_launcher.py

# Verify hash matches expected
sha256sum ~/.local/bin/skfleet-rotate.py | grep -q 36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e && echo "OK" || echo "FAIL"

# Dry run the rotation (safe, no mutations)
~/.local/bin/skfleet-rotate.py

# Check syntax
python3 -m py_compile ~/.local/bin/skfleet-rotate.py
```

## Service Restart (If Required)

The rotation runs as a systemd timer. If the launcher binary was replaced while the service was running:

```bash
# Check rotation status
systemctl status skfleet-rotate.timer
systemctl status skfleet-rotate.service

# The service runs oneshot and exits; no restart needed unless actively running
# If needed, trigger a manual run:
systemctl start skfleet-rotate.service

# Check logs
journalctl -u skfleet-rotate.service -n 50
```

## Rollback Decision Points

### When to Rollback

1. **Immediate rollback required if:**
   - Rotation service fails to start
   - Workers are not being launched
   - Claims are not being released properly
   - Evidence files are corrupted
   - Tests fail with critical errors

2. **Consider rollback if:**
   - Performance degradation observed
   - Unexpected NOOP behavior on all hosts
   - Claim deadlocks detected
   - Evidence writes failing

### When NOT to Rollback

- Test failures that are test issues (not launcher issues)
- Cosmetic or documentation-only changes
- Changes that improve safety or correctness without breaking functionality

## Safety Checks Before Rollback

1. **Verify the problem is actually with the launcher:**
   ```bash
   # Check for other causes
   systemctl status skfleet-rotate.timer
   tmux ls  # Check for stuck sessions
   df -h    # Check disk space
   ```

2. **Confirm the rollback target is valid:**
   ```bash
   # Verify hash of rollback target
   sha256sum <rollback-target>
   ```

3. **Backup current state before rollback:**
   ```bash
   cp ~/.local/bin/skfleet-rotate.py ~/.local/bin/skfleet-rotate.py.before-rollback-$(date +%Y%m%dT%H%M%SZ)
   ```

## Contact Points

- **Rotation service:** `systemctl status skfleet-rotate.*`
- **Fleet logs:** `~/.skcapstone/fleet/logs/`
- **Evidence directory:** `~/.skcapstone/evidence/`
- **SKCoord status:** `skcapstone coord status`

## Evidence Preservation

All rollback actions should preserve evidence:

1. Keep a backup of the broken version
2. Log the reason for rollback
3. Record the hash before and after
4. Save test results from both states

## Appendix: Hash Reference

| Location | SHA-256 | Status |
|----------|---------|--------|
| Installed launcher (`~/.local/bin/skfleet-rotate.py`) | `36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e` | VERIFIED |
| Source repository (`scripts/fleet/skfleet-rotate.py` at 06dfa7e) | `36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e` | VERIFIED |
| Worktree copy | `36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e` | VERIFIED |

---

**Document Version:** 1.0
**Created:** 2026-08-28T16:00:00Z
**Card:** ff68bade
