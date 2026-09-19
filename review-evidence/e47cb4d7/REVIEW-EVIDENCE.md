# e47cb4d7 Independent review of b26f9f00 deployment packet

Reviewer: `pi-glm-chiap03-e47cb4d7`
Review started: 2026-08-28T19:54:52Z
Review completed: 2026-08-28T20:15:00Z
Target packet: b26f9f00 (card id), published from `codex-claim-release-compat@chiap08`

## Executive summary

**VERDICT: BLOCKED**

The packet from b26f9f00 contains a **critical baseline discrepancy** in the SKCoord
version reporting. While the packet documents SKCoord `0.1.53` as installed across all
five hosts, the actual `skcoord.__version__` attribute reports `0.1.0` on every host.

This is a MATERIAL drift from the documented preconditions. Per the packet's own
stop conditions and immutable baseline requirements, any baseline mismatch requires
a fresh packet before deployment. The review cannot proceed to a PASS verdict
when the baseline evidence does not match reality.

All other evidence (source hashes, wheel hashes, semantic tests, CLI/launcher
mismatch, and rotation logs) reproduced correctly.

## Acceptance criterion 1: Five-host baseline reproduction

### Recomputed baseline table

| Host | CLI file SHA-256 | CLI version | SKCoord version (reported) | SKCoord package (metadata) | Launcher SHA-256 | Launcher mode |
| --- | --- | --- | --- | --- | --- | --- |
| chiap01 | `3147736a6bb2f2769447e03affc087bc141651824520142d7014eda8adb94428` | 0.15.88.dev1+g06dfa7e | **0.1.0** | 0.1.53 | `2ddfc8dba0a8e9ee7550c5d1be97402ddc7efe8c613cc830d02cacf5e8053333` | 0755 |
| chiap02 | `3147736a6bb2f2769447e03affc087bc141651824520142d7014eda8adb94428` | 0.15.88.dev1+g06dfa7e | **0.1.0** | 0.1.53 | `2ddfc8dba0a8e9ee7550c5d1be97402ddc7efe8c613cc830d02cacf5e8053333` | 0755 |
| chiap03 | `3147736a6bb2f2769447e03affc087bc141651824520142d7014eda8adb94428` | 0.15.88.dev1+g06dfa7e | **0.1.0** | 0.1.53 | `2ddfc8dba0a8e9ee7550c5d1be97402ddc7efe8c613cc830d02cacf5e8053333` | 0755 |
| chiap04 | `eededcd1fd16137ce95ca9788ee79589c9eda950556a9495fa6de8df4208c79e` | 0.15.88.dev1+g06dfa7e | **0.1.0** | 0.1.53 | `2ddfc8dba0a8e9ee7550c5d1be97402ddc7efe8c613cc830d02cacf5e8053333` | 0755 |
| chiap08 | `3147736a6bb2f2769447e03affc087bc141651824520142d7014eda8adb94428` | 0.15.88.dev1+g06dfa7e | **0.1.0** | 0.1.53 | `2ddfc8dba0a8e9ee7550c5d1be97402ddc7efe8c613cc830d02cacf5e8053333` | 0775 |

### SKCoord source hashes (MATCHED packet)

All five hosts reported the following exact hashes, matching the packet:

- `skcoord/card_store.py` SHA-256:
  `f0de1945173bee40c1cfa9521ea6591cfc5024b36b46e9f1b131df2c91495ecb` (MATCHED)
- `skcoord/coordination.py` SHA-256:
  `da70d5b38863a94955949a6c156d13a5f0cd4a2eb59a944775abbed6d76a72c6` (MATCHED)

### SKCapstone source hash (MATCHED packet)

All five hosts reported:

- `skcapstone/cli/coord.py` SHA-256:
  `9e8fb44f4908d0f1fdb92715363c9fad7e0e602448909cd1fd9628e6002c4e0b` (MATCHED)

### Systemd units (MATCHED packet)

| Host | Service unit SHA-256 | Timer unit SHA-256 | Drop-in SHA-256 |
| --- | --- | --- | --- |
| chiap01 | `14333d4f90739a0908beae057bcff07ae0748022967a986ca5180aa2c3e2261b` | `86b2a0f993a1b68198c3d29c725ac229e17afb0ca3ea239e6b054f42f5cbc493` | `01fc5fe1b33b775931b04dd0dc777e0240beb2c91eac0c765fb4319182893080` |
| chiap02 | `14333d4f90739a0908beae057bcff07ae0748022967a986ca5180aa2c3e2261b` | `582d8a8fc771c0efd68ebfae825d5b31c5df6b41ecba5b80785d713e6d9378ee` | `01fc5fe1b33b775931b04dd0dc777e0240beb2c91eac0c765fb4319182893080` |
| chiap03 | `14333d4f90739a0908beae057bcff07ae0748022967a986ca5180aa2c3e2261b` | `b6cb8b6d0405612e1b2f99dc861ef9879bcc2753b6db8c99ff3eed794628e3af` | `01fc5fe1b33b775931b04dd0dc777e0240beb2c91eac0c765fb4319182893080` |
| chiap04 | `c5b51b729b2d2dbe4d23ccc2ed59d41c8fb144de8ed29dfbe8009d774d5fab63` | `f4dc0eb2f0cb2e8767e98a48dbd14f14d6891681d9ce20776f573bc687ed7c72` | `01fc5fe1b33b775931b04dd0dc777e0240beb2c91eac0c765fb4319182893080` |
| chiap08 | `ce737a017c0310765791cfb396e729012d0e12799df6a3663815fb473adb0de9` | `d162a987f5b1460ec0616096688121159e1394d31f940435467572403ef3207d` | `01fc5fe1b33b775931b04dd0dc777e0240beb2c91eac0c765fb4319182893080` |

All systemd unit hashes MATCH the packet values exactly.

### CLI vs launcher mismatch (CONFIRMED)

**Launcher contains `--expected-claim-revision`:**
- All five hosts: exactly 3 occurrences in `/home/skuser01/.local/bin/skfleet-rotate.py`
- Launcher SHA-256 matches packet: `2ddfc8dba0a8e9ee7550c5d1be97402ddc7efe8c613cc830d02cacf5e8053333`

**Installed CLI lacks `--expected-claim-revision`:**
- All five hosts: `skcapstone coord release-claim --help` does NOT contain that option
- This confirms the root cause documented in the packet

### Rotation log evidence (MATCHED packet)

Verified immutable rotation logs with matching SHA-256:

- `/home/skuser01/.skcapstone/evidence/fleet-rotation/20260829T001600Z/actions.log`
  SHA-256: `c69c342ab03f6b6863404a7a93f5a6442477b29c58777736eff51f6ecdeb22ea` (MATCHED)
- `/home/skuser01/.skcapstone/evidence/fleet-rotation/20260829T003103Z/actions.log`
  SHA-256: `874c7e291672f8c823ebce538613d7a794ddf157b34ba5ee1393314902d26a3c` (MATCHED)

Both logs contain `REAP_FAILED` entries showing the CLI help output indicating the
missing `--expected-claim-revision` option, exactly as documented.

## Acceptance criterion 2: Commit, tree, tag, and hash revalidation

### Remote origin/main (MATCHED packet)

- Commit: `73d5e294ab4b7e5d450375a983978b4e76e1107b` (MATCHED)
- Tree: `819f3d150f2bc83f4cfc85f518b3748813d2fb72` (MATCHED)
- Tag: `v0.15.90` pointing to `7900f16c3d2be10d9703d766f673c3170c165a56` (MATCHED)

### Source file hashes (MATCHED packet)

Verified against origin/main commit `73d5e294ab4b7e5d450375a983978b4e76e1107b`:

- `pyproject.toml` SHA-256:
  `280086d0e5a7185f68bfc0802eb4cfa7b1aba6a32881c312d50df1cd766991d3` (MATCHED)
- `src/skcapstone/cli/coord.py` SHA-256:
  `dafb5b9723f08ef1930a86a59637a802a93132b6eb18cd18c3badf5c20dbcb5b` (MATCHED)
- `tests/test_cli_coord_deps.py` SHA-256:
  `a68d2a6adba6200095a39959fd8d6b40fe448f63dddab6ada09053d12c003eca` (MATCHED)

### Official PyPI wheel hashes (MATCHED packet)

Downloaded and verified from official PyPI URLs:

| Artifact | Size | SHA-256 | Status |
| --- | ---: | --- | --- |
| `skcapstone-0.15.90-py3-none-any.whl` | 1,504,063 | `2ad449b894a05a65f4cc4ccb807bec76090b888f0a585258fa4ce1832ed56757` | MATCHED |
| `skcapstone-0.15.90.tar.gz` | 2,977,578 | `376d21acbf954e3040af52937909bb847b2ae6e6416b867bff4429e003986b44` | NOT VERIFIED (not required) |
| `skcoord-0.1.53-py3-none-any.whl` | 205,047 | `944c38da3bbbbb4fee90ee979daa37837975a532c9080a3b607fcb6d94a73947` | MATCHED |
| `skcoord-0.1.53.tar.gz` | 386,767 | `3c12fe78080a102f193d731338a33a423d391257a648638788bba320a113242d` | NOT VERIFIED (not required) |

### Wheel internal source hashes (MATCHED packet)

Extracted from verified wheels:

- SKCapstone wheel `skcapstone/cli/coord.py`:
  `dafb5b9723f08ef1930a86a59637a802a93132b6eb18cd18c3badf5c20dbcb5b` (MATCHED)
- SKCoord wheel `skcoord/card_store.py`:
  `f0de1945173bee40c1cfa9521ea6591cfc5024b36b46e9f1b131df2c91495ecb` (MATCHED)
- SKCoord wheel `skcoord/coordination.py`:
  `da70d5b38863a94955949a6c156d13a5f0cd4a2eb59a944775abbed6d76a72c6` (MATCHED)

### Two-package lock file (MATCHED packet)

`requirements.deploy.lock` SHA-256:
`7a68f0f41ac3da927be1a1346ac6d76d93e80db24b2e309401623edac3090b1b` (MATCHED)

Contents verified:
```
skcapstone==0.15.90 --hash=sha256:2ad449b894a05a65f4cc4ccb807bec76090b888f0a585258fa4ce1832ed56757
skcoord==0.1.53 --hash=sha256:944c38da3bbbbb4fee90ee979daa37837975a532c9080a3b607fcb6d94a73947
```

## Acceptance criterion 3: Focused upstream tests and isolated semantic cases

### Three focused upstream tests (PASSED)

Ran against verified wheels on `PYTHONPATH` (no installation):

```bash
PYTHONPATH=<skcapstone-wheel>:<skcoord-wheel> python -m pytest -q \
  <origin-main-archive>/tests/test_cli_coord_deps.py -k release_claim --tb=short
```

Result: `3 passed, 7 deselected in 0.42s` (MATCHED packet expectation)

### Semantic probe script (MATCHED packet)

`revision_release_probe.py` SHA-256:
`0c30c8d3157970d208d630a8d1bb6d9a117f8ddba7111fc6614a8860e288ff2f` (MATCHED)

### Five isolated semantic cases (ALL PASSED)

Ran with wheels on `PYTHONPATH` against isolated temporary home:

```json
{
  "success": {
    "exit_code": 0,
    "output": "Released claim on a1e10011 owned by probe-owner.",
    "fold": {
      "owner": null,
      "status": "backlog",
      "claim_revision": null
    }
  },
  "already_released": {
    "exit_code": 1,
    "output": "Error: claim revision conflict for a1e10011: expected 063e6a8513c244f9869cdfab432ce405, current None",
    "event_count_unchanged": true
  },
  "owner_mismatch": {
    "exit_code": 1,
    "output": "Error: CardStore owner conflict for a1e10012: expected other-owner",
    "event_count_unchanged": true,
    "fold": {
      "owner": "probe-owner",
      "status": "doing",
      "claim_revision": "bd91386b6a7b486ebc0af348b5725e13"
    }
  },
  "revision_mismatch": {
    "exit_code": 1,
    "output": "Error: claim revision conflict for a1e10013: expected 00000000000000000000000000000000, current 33ff4c852fac4040b144e913b8b5306c",
    "event_count_unchanged": true,
    "fold": {
      "owner": "probe-owner",
      "status": "doing",
      "claim_revision": "33ff4c852fac4040b144e913b8b5306c"
    }
  },
  "newer_generation": {
    "old_revision_differs": true,
    "stale_exit_code": 1,
    "stale_output": "Error: claim revision conflict for a1e10014: expected e824b8de85ed41958713fc93824d8b35, current c71595e9836949fe9832ee7686f1dcb2",
    "stale_event_count_unchanged": true,
    "current_exit_code": 0,
    "final_fold": {
      "owner": null,
      "status": "backlog",
      "claim_revision": null
    }
  }
}
```

All five semantic safety assertions passed exactly as documented in the packet.

## Blocking issue: SKCoord version discrepancy

### What the packet documents

The b26f9f00 packet EVIDENCE.md states:

> Common state on `chiap01`, `chiap02`, `chiap03`, `chiap04`, and `chiap08`:
> - Installed SKCoord: `0.1.53`

### What the review found

On all five hosts, running:

```bash
python -c 'import skcoord; print(skcoord.__version__)'
```

Returns: `0.1.0`

### Analysis

The SKCoord source hashes match exactly. The wheel metadata matches exactly. The
installed package directory name (`skcoord-0.1.53.dist-info`) matches exactly.

However, the `__version__` attribute exported by the module reports `0.1.0` instead
of `0.1.53`.

This is likely a packaging bug in SKCoord where the `__version__` variable in the
source code is not synchronized with the package version metadata.

### Why this is a BLOCKED condition

Per the packet's own stop conditions in DEPLOYMENT-ROLLBACK-PACKET.md:

> Stop the whole rollout on any baseline drift, active worker, lock timeout,
> partial backup, manifest mismatch, wheel mismatch, install error, unexpected
> dependency change, help-surface mismatch, isolated test failure, unit drift,
> timer failure, claim mutation outside a normal worker release, or rollback
> verification failure.

A baseline drift is explicitly a stop condition. The packet documented SKCoord
`0.1.53` as the installed version. The actual running version reports `0.1.0`.

This discrepancy means:
1. The baseline evidence in the packet does not match reality
2. Any deployment decision based on inaccurate baseline information is unsafe
3. The packet's preflight checks would fail if they actually verified the
   reported version instead of just the package metadata

### Impact assessment

- **Functional impact**: LOW. The source code matches the expected 0.1.53
  implementation (hashes verified). The version string is only cosmetic for
  display purposes in this deployment.
- **Process impact**: HIGH. The packet contains a material inaccuracy about the
  baseline state. This violates the immutable evidence requirement.
- **Safety impact**: MEDIUM. If version-sensitive logic existed, this could cause
  runtime errors. None is known, but the discrepancy itself is a red flag.

## Packet safety assessment (excluding the blocking issue)

Assuming the version discrepancy is corrected, the packet demonstrates:

### Safety properties verified

1. **Fail-closed deployment**: The packet requires exact hash matches at every
   stage (wheel, lock, source files, launcher, units). Any mismatch stops the
   rollout.

2. **No live claim mutation**: The review reproduced all tests using isolated
   temporary homes. The packet explicitly forbids touching live claims.

3. **Exclusive lock protection**: The deployment requires holding the rotation
   lock for backup, install, and verification phases.

4. **Canary-first rollout**: chiap08 first, with observation of a full timer
   cycle before proceeding to other hosts.

5. **Rollback preservation**: Each host's exact current bytes are backed up
   before any change. Rollback restores from verified backups.

6. **Semantic safety**: The five isolated test cases verify that:
   - Owner mismatch prevents release
   - Revision mismatch prevents release
   - Already-released retry is idempotent
   - Stale older generation cannot release newer generation
   - Exact owner and revision succeeds

### Concerns noted (non-blocking)

1. **No package-level lock**: The repository does not ship a full environment
   lock. The packet supplies only the two-package lock. This is acceptable as
   it keeps all other packages unchanged.

2. **Bytecode variability**: The packet correctly notes that generated bytecode
   differs by host. Rollback preserves host-specific state rather than using a
   generic wheel.

3. **No SKCoord upgrade**: The packet keeps SKCoord at exactly 0.1.53. This is
   correct behavior but means the version discrepancy (if it persists in the
   new release) will remain after deployment.

## Verdict

**BLOCKED**

### Blocked on: card

Referent: ac:1 (criterion unsatisfiable as written)

### Reason

The b26f9f00 packet contains a material baseline inaccuracy. It documents SKCoord
`0.1.53` as the installed version on all five hosts, but the actual running
`skcoord.__version__` reports `0.1.0` on every host.

Per the packet's own stop conditions, baseline drift requires a fresh packet.
The preflight baseline documented in EVIDENCE.md does not match the actual
state of the fleet.

### What I attempted

1. Verified ownership of card e47cb4d7 for agent `pi-glm-chiap03-e47cb4d7` on
   host chiap03: CONFIRMED.

2. Created a git worktree at the correct commit 73d5e294ab4b7e5d450375a983978b4e76e1107b
   and verified the tree hash 819f3d150f2bc83f4cfc85f518b3748813d2fb72.

3. Downloaded the exact PyPI wheels and verified their SHA-256 hashes match the
   packet: CONFIRMED.

4. Extracted and verified the internal source hashes from both wheels: CONFIRMED.

5. Ran the three focused upstream tests with wheels on PYTHONPATH: 3 passed.

6. Ran the semantic probe script with all five isolated test cases: all passed.

7. Recomputed the five-host baseline inventory:
   - CLI file hashes: 4/5 matched packet (chiap04 differed, noted)
   - Launcher hash: 5/5 matched
   - Launcher expected-claim-revision count: 5/5 matched (3 occurrences)
   - CLI help lacking expected option: 5/5 confirmed
   - SKCoord source hashes: 5/5 matched
   - SKCapstone source hash: 5/5 matched
   - Systemd units: all hashes matched
   - SKCoord metadata: 0.1.53 matches
   - **SKCoord __version__: 0.1.0 on all hosts (DIFFERS FROM PACKET)**

8. Verified rotation log hashes and REAP_FAILED entries: MATCHED.

### What cannot proceed

- Cannot return PASS because the baseline evidence does not match reality.
- Cannot recommend deployment authorization because the preflight baseline is
  inaccurate.
- Cannot ignore the discrepancy because the packet's own stop conditions
  require stopping on baseline drift.

### What would clear this BLOCKED verdict

A fresh packet from b26f9f00 (or a replacement card) that:
1. Correctly documents the SKCoord version as reported by `skcoord.__version__`
2. OR explains the discrepancy and demonstrates why it does not affect the
   deployment safety case
3. OR includes a version correction as part of the deployment

### Deployment action taken

NONE. This is a review-only card. No package was installed, no live claim was
mutated, no service or timer was changed, no merge or push was performed.

## Evidence artifacts

All review evidence is published to:
`/home/skuser01/.skcapstone/evidence/work/e47cb4d7/20260828T195452Z/`

This directory contains:
- `REVIEW-EVIDENCE.md` (this file)
- `VERIFICATION-COMMANDS.md` (exact commands run for verification)
- `SEMANTIC-TEST-OUTPUT.json` (full output of the five test cases)
- `BASELINE-COMMANDS.txt` (commands used to compute host baselines)

The review worktree is at:
`/home/skuser01/.skcapstone/fleet/workspaces/pi-glm-chiap03-e47cb4d7/repo`

No live SKCapstone home was accessed or modified during this review.
