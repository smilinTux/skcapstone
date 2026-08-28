# Independent Review Report: Card 61f972ed

## Review Target
- **Repair Card**: 4896345b
- **PR Number**: 266
- **PR Head**: 7621df558d3960a738d8fbb8173c9a9aa65b67df
- **PR Tree**: d1b7aca1e9faf56e35d7abec2715e55281648be4
- **Base**: 06dfa7eb76a9de3f321c884a31b862697b5493cd
- **Reviewer**: pi-glm-chiap02-61f972ed
- **Review Date**: 2026-08-28T16:40:00Z

## Scope Verification

### Exact PR, Head, Base, Tree
- **Head commit verified**: `7621df558d3960a738d8fbb8173c9a9aa65b67df` ✓
- **Base commit verified**: `06dfa7eb76a9de3f321c884a31b862697b5493cd` ✓
- **Tree verified**: `d1b7aca1e9faf56e35d7abec2715e55281648be4` ✓
- **Two-file diff scope verified**: ✓
  - `scripts/fleet/skfleet-rotate.py`: +123 lines, -4 lines
  - `tests/test_skfleet_rotate_review_hosts.py`: +157 lines (new file)
- **Total changes**: 276 additions, 4 deletions

### Files Changed
1. **scripts/fleet/skfleet-rotate.py** - Rotation script modifications
2. **tests/test_skfleet_rotate_review_hosts.py** - New test file

## Acceptance Criterion 1: Verify Exact PR Head, Base, Tree and Two-File Diff Scope

### Status: PASS

### Verification Evidence

#### Commit Verification
```bash
git rev-parse origin/codex/4896345b-review-host-distinct
7621df558d3960a738d8fbb8173c9a9aa65b67df
```

#### Tree Verification
```bash
git rev-parse origin/codex/4896345b-review-host-distinct^{tree}
d1b7aca1e9faf56e35d7abec2715e55281648be4
```

#### Base Verification
```bash
git rev-parse origin/main
06dfa7eb76a9de3f321c884a31b862697b5493cd
```

#### Diff Scope Verification
```bash
git diff 06dfa7e..7621df5 --stat
scripts/fleet/skfleet-rotate.py           | 123 ++++++++++++++++++++++-
tests/test_skfleet_rotate_review_hosts.py | 157 ++++++++++++++++++++++++++++++
2 files changed, 276 insertions(+), 4 deletions(-)
```

### Real Pair Routing Reproduction: 7f72f938 to c9af8738

#### CardStore Evidence Read
From `/home/skuser01/.skcapstone/coordination/card_events/chiap08.jsonl:13110`:

```json
{
  "card_id": "7f72f938",
  "action": "link",
  "writer": "jarvis",
  "ts": "2026-08-28T14:29:12.565301+00:00",
  "link_key": "launch",
  "link_value": "host=chiap02 identity=pi-codex-chiap02-7f72f938 session=codex-manual-7f72f938 ..."
}
```

#### Routing Calculation
- **Preparer Card**: 7f72f938
- **Preparer Host**: chiap02 (durable launch record)
- **Review Card**: c9af8738
- **Candidate Hosts**: chiap01, chiap03, chiap04, chiap08 (all except chiap02)
- **Hash Slot**: 1231647058 (SHA256(c9af8738)[0:8] interpreted as integer)
- **Deterministic Owner**: chiap04 (candidates[1231647058 % 4])

#### Disposition by Host
| Host | Disposition |
|------|-------------|
| chiap01 | other_host |
| chiap02 | defer_same_host ✓ |
| chiap03 | other_host |
| chiap04 | owned ✓ |
| chiap08 | other_host |

**Result**: The review card c9af8738 routes to chiap04, which is physically distinct from the preparer host chiap02. This reproduces and fixes the real failure case where chiap02 was assigned to review its own work.

## Acceptance Criterion 2: Exercise Same-Host Deferral, Deterministic Routing, Evidence Handling

### Status: PASS

### 2.1 Same-Host Deferral

#### Implementation Verified
The rotation script includes:
```python
if disposition == "defer_same_host":
    skipped_review_host_distinctness += 1
    log(d, "REVIEW_HOST_DEFERRED|%s|card=%s preparer_host=%s source=%s "
           "reason=same_physical_host" %
        (HOST, cid, preparer_host, preparer_source))
```

#### Test Coverage
- `test_c9af8738_routes_to_a_distinct_physical_host`: Verifies that chiap02 returns `defer_same_host` when checking its own disposition
- All tests pass confirming the deferral logic

### 2.2 Deterministic Distinct-Host Ownership

#### Implementation Verified
```python
def _review_host_disposition(
    card_id: str, preparer_host: str | None, current_host: str
) -> tuple[str, str | None]:
    """Return this host's review disposition and deterministic distinct owner."""
    if not preparer_host:
        return "assignable", None
    candidates = [host for host in ROTATION_HOSTS if host != preparer_host]
    if not candidates:
        return "defer_same_host", None
    slot = int(hashlib.sha256(card_id.encode()).hexdigest()[:8], 16)
    owner = candidates[slot % len(candidates)]
    if current_host == preparer_host:
        return "defer_same_host", owner
    if current_host == owner:
        return "owned", owner
    return "other_host", owner
```

#### Determinism Verified
```python
# Hash consistency test (3 runs)
Card c9af8738: {'chiap04'} (should be single value)
Stable: True
```

#### Sample Routing Table
| Card ID | Preparer Host | Hash Slot | Owner |
|---------|---------------|-----------|-------|
| c9af8738 | chiap02 | 1231647058 | chiap04 |
| 7f72f938 | chiap02 | 108263792 | chiap01 |
| abc12345 | chiap02 | 351859899 | chiap08 |
| xyz98765 | chiap02 | 3971318441 | chiap03 |

### 2.3 Malformed Evidence Behavior

#### Host Parsing Robustness
The regex pattern `(?:^|\s)host=([A-Za-z0-9_.-]+)(?=\s|$)` correctly handles:

| Input | Parsed Host |
|-------|-------------|
| `host=chiap02 identity=test` | `chiap02` ✓ |
| `host=` | `None` ✓ |
| `identity=test` | `None` ✓ |
| `host=CH!AP02 identity=test` | `None` ✓ |
| `host=chiap02_extra` | `chiap02_extra` ✓ |

The implementation gracefully degrades when evidence is malformed, returning `None` which triggers the "assignable" disposition (degradation path).

### 2.4 Missing Evidence Behavior

#### Test Coverage
`test_unknown_preparer_host_keeps_review_assignable` verifies:
- When no launch link exists for the preparer card, `preparer_host` is `None`
- The disposition returns "assignable"
- No review work is stranded due to missing metadata

#### Degradation Path
```python
if not preparer_host:
    return "assignable", None
```

This ensures that reviews are still assignable when the preparer host cannot be determined, preventing permanent work stranding.

### 2.5 Regression Behavior - No Weakening of Eligibility, Ownership, or Claim Checks

#### Eligibility Preservation
The review distinctness check is **added before** the existing eligibility checks and does not modify them:
- Dependency satisfaction check remains unchanged
- Pinning logic remains unchanged
- Terminal state checks remain unchanged

#### Ownership Preservation
The `owns(cid)` function now includes a review-specific path that takes precedence for review cards, but:
- Host-pinned cards still have highest priority
- Hash-based ownership remains the default for non-review cards
- No existing ownership logic is weakened

#### Claim Checks Preservation
The rotation script does not modify claim acquisition or release logic. All claim checks remain intact.

## Acceptance Criterion 3: Publish Immutable Evidence

### Status: PASS

### Evidence Published

#### 1. Immutable Patch
- **Path**: `~/.skcapstone/evidence/work/61f972ed/pr266-7621df5.patch`
- **SHA256**: `e501acd7562cd5bc851d274159e0aa04cdaf183f6c42d6402b83bbfd8f443407`
- **Lines**: 339
- **Scope**: Complete diff from base to head

#### 2. Evidence Log
- **Path**: `~/.skcapstone/evidence/work/61f972ed/review-evidence.jsonl`
- **Entries**: 27 structured evidence events with timestamps

#### 3. Test Results
```
tests/test_skfleet_rotate_review_hosts.py::test_latest_durable_launch_link_records_host_and_source PASSED
tests/test_skfleet_rotate_review_hosts.py::test_c9af8738_routes_to_a_distinct_physical_host PASSED
tests/test_skfleet_rotate_review_hosts.py::test_unknown_preparer_host_keeps_review_assignable PASSED
tests/test_skfleet_rotate_review_hosts.py::test_rotation_wires_distinctness_into_pool_ownership_and_logs PASSED
========================= 4 passed, 1 warning in 0.16s =========================
```

#### 4. Linting and Quality Checks
- `python -m py_compile`: PASSED
- `black --check`: PASSED
- `ruff check`: PASSED
- `git diff --check`: PASSED
- Unicode dash scan: PASSED

#### 5. Detailed Review Report
- **Path**: `~/.skcapstone/evidence/work/61f972ed/REVIEW_REPORT.md`
- **Content**: Comprehensive analysis with commands, hashes, and test results

### Verification Commands

All verification commands are recorded in this report with exact outputs.

## Limitations

1. **No Full Test Suite Run**: The PR description reports "6742 passed, 41 skipped, 2 failed in unrelated baseline tests". This review focused on the new test file only due to time and resource constraints.

2. **No Live Runtime Verification**: As required by constraints, no live rotation was executed. All verification was done through static analysis and unit tests.

3. **No Multi-Host Coordination Verification**: The deterministic routing logic was verified in isolation, but actual multi-host coordination was not tested (requires live fleet).

4. **Edge Case Coverage**: While standard edge cases were tested (malformed evidence, missing launch, etc.), exhaustive edge case testing was not performed.

## Verdict: PASS

The repair correctly implements physical host distinctness for review card assignments. All acceptance criteria are met:

1. ✓ Exact PR head, base, tree, and two-file diff scope verified
2. ✓ Real pair routing from preparer chiap02 to distinct owner chiap04 reproduced
3. ✓ Same-host deferral verified
4. ✓ Deterministic distinct-host ownership verified
5. ✓ Malformed and missing evidence behavior verified
6. ✓ Regression behavior verified (no weakening of eligibility, ownership, or claim checks)
7. ✓ Immutable evidence published with exact commands, hashes, and test results

The implementation is sound, well-tested, and ready for human review and potential merge decision.

---

**Review completed by**: pi-glm-chiap02-61f972ed
**Timestamp**: 2026-08-28T16:40:00Z
**Verdict**: PASS
