# Independent Review Report for Card bd795bb2

**Review Date:** 2026-08-29T01:03:00Z
**Reviewer Agent:** pi-glm-chiap08-bd795bb2
**Source Card:** 486eabd1
**Source Agent:** codex-backoff-repair
**Review Type:** Independent Source Review (no runtime mutation)

---

## Executive Summary

**VERDICT: PASS**

The candidate patch SHA256 `426f42430643f941aede4db86b90ad2540ae6007d8a250ddfa865be9288d538e` from base commit `73d5e294ab4b7e5d450375a983978b4e76e1107b` to candidate tree `4796d84b749eb702f17dce039baea828885b5995` successfully implements edge-triggered wake logic for the fleet BLOCKED pool. All acceptance criteria are met, all file hashes reproduce exactly, and all focused regression tests pass (115/115).

### Key Confirmations

1. **Hash Reproduction:** All source handoff hashes verified against the repository
2. **Claim-Fenced Retry:** `_WAKE_RETRY_LIMIT=1` enforced via `_wake_retry_available()`
3. **Metadata Validation:** Mixed/malformed blocker reasons fail closed via `_blocked_reason()`
4. **Human Gate Protection:** Machine PASS evidence cannot discharge human gates via `_human_resolution_epoch()`
5. **Dependency Edge Verification:** Cards 83e04cf1 and ad5f9d7b remain parked without structural dependency edges
6. **PASS Variants:** PASS, PASS_FOR_REVIEW, and PASS_FOR_REREVIEW remain awaiting review

---

## Acceptance Criterion 1: Hash Reproduction

### Base Commit and Tree
```bash
$ git rev-parse 73d5e294ab4b7e5d450375a983978b4e76e1107b
73d5e294ab4b7e5d450375a983978b4e76e1107b
73d5e29 Return cards whose recorded blocker has since completed (#288)

$ git rev-parse 73d5e294ab4b7e5d450375a983978b4e76e1107b^{tree}
819f3d150f2bc83f4cfc85f518b3748813d2fb72 ✓
```

### Candidate Tree
```bash
$ git show 4796d84b749eb702f17dce039baea828885b5995:scripts/fleet/skfleet-rotate.py | sha256sum
e6c9faa703c8700e0b64b9b0a96b08728a137edcb1607137a25bb1af47eeb625 ✓

$ git show 4796d84b749eb702f17dce039baea828885b5995:tests/test_skfleet_backoff_wake.py | sha256sum
766ca141dd921cfc47021dd5e9addd3ebc2870904483019dbf2297ea03b5d2b0 ✓
```

### File Hashes Verified Against Source Handoff

| File | Base SHA256 | Candidate SHA256 | Status |
|------|-------------|------------------|--------|
| scripts/fleet/skfleet-rotate.py | 2ddfc8dba0a8e9ee7550c5d1be97402ddc7efe8c613cc830d02cacf5e8053333 | e6c9faa703c8700e0b64b9b0a96b08728a137edcb1607137a25bb1af47eeb625 | ✓ |
| tests/test_skfleet_backoff_wake.py | (new file) | 766ca141dd921cfc47021dd5e9addd3ebc2870904483019dbf2297ea03b5d2b0 | ✓ |

### Patch SHA256
```bash
$ sha256sum candidate.patch
426f42430643f941aede4db86b90ad2540ae6007d8a250ddfa865be9288d538e ✓
```

**Result:** ALL HASHES REPRODUCE EXACTLY

---

## Acceptance Criterion 2: Metadata and Generation Review

### Blocker Category Validation

The `_blocked_reason()` function enforces strict category and referent validation:

```python
def _blocked_reason(val):
    # ... parsing logic ...
    categories = list(dict.fromkeys(categories))
    if len(categories) != 1:  # MIXED CATEGORIES FAIL CLOSED
        return None
    # ... must have at least one referent ...
    return (categories[0], tuple(dict.fromkeys(refs))) if refs else None
```

**Verification:**
- `parse("BLOCKED|card|card:482cc241")` → `("card", ("card:482cc241",))` ✓
- `parse("BLOCKED blocked_on=card referent=card:482cc241 and referent=card:2076c423")` → `("card", ("card:482cc241", "card:2076c423"))` ✓
- `parse("BLOCKED blocked_on=card")` → `None` (no referent) ✓
- `parse("BLOCKED blocked_on=card referent=ac:1 blocked_on=human referent=approval:x")` → `None` (mixed categories) ✓

### Claim-Fenced Retry Enforcement

```python
_WAKE_RETRY_LIMIT = 1

def _wake_retry_available(cid, generation):
    """One exact claim-fenced retry per blocker generation."""
    retries = sum(1 for launched in _wake_launch_times.get(cid, ()) if launched > generation)
    return retries < _WAKE_RETRY_LIMIT
```

**Verification via test `test_exact_567_timeline_wakes_then_exhausts_one_generation`:**
1. Card 567e6b09 blocked on card:482cc241
2. After 482cc241 completes, wake becomes available
3. First wake launch does NOT consume retry (no claim revision recorded)
4. Second wake with `_wake_launch_times[card] = [epoch]` exhausts retry
5. New BLOCKED verdict begins new generation

**Result:** ONE CLAIM-FENCED RETRY PER GENERATION ENFORCED ✓

### Human Gate Protection

The `_human_resolution_epoch()` function requires explicit human authority:

```python
def _human_resolution_epoch(cid, referent, threshold):
    # ...
    actor = str(event.get("writer") or "").lower()
    authorized = actor in ("chef", "human") or "human-decision-recorder" in actor
    direct = bool(re.search(r"\b(APPROVE(?:D)?|VOID)\b", blob, re.I))
    # ... only authorized actors with APPROVE or VOID trigger wake ...
```

**Verification via test `test_human_wake_requires_exact_explicit_approval_or_void`:**
- `link_key="successor-review" link_value="PASS"` → CARD REMAINS BLOCKED ✓
- `link_key="human-decision" link_value="APPROVE ... by Chef"` → CARD WAKES ✓

**Result:** SUCCESSOR MACHINE PASS NEVER DISCHARGES A HUMAN GATE ✓

---

## Acceptance Criterion 3: Timeline Test and Parked Cards

### Exact 567e6b09 Timeline Test Reproduction

```python
def test_exact_567_timeline_wakes_then_exhausts_one_generation():
    card = "567e6b09"
    referent = "482cc241"
    verdict = "2026-08-28T11:11:59.211280+00:00"
    resolved = "2026-08-28T11:26:03.352672+00:00"
    # ... setup ...
    board.outcome(card, verdict, "BLOCKED blocked_on=card referent=card:482cc241")

    # Historical 20:02 launch had no claim revision
    board.ns["_launched_at"][card] = board.epoch("2026-08-28T20:02:00Z")
    assert board.blocked(card) is False  # Wake available
    assert board.blocked(card) is False  # Still available

    # Claim-fenced launch exhausts retry
    board.ns["_wake_launch_times"][card] = [board.epoch("2026-08-28T20:30:00Z")]
    assert board.blocked(card) is True  # Retry exhausted

    # New BLOCKED verdict begins new generation
    board.outcome(card, "2026-08-28T20:31:00Z", "BLOCKED|card|card:482cc241")
    assert board.blocked(card) is True  # Parked on new generation
```

**Test Result:** PASSED ✓

### Cards 83e04cf1 and ad5f9d7b Remain Parked

**Card 83e04cf1 Analysis:**
- Title: "[FLEET-UNBLOCK-WAVE-01-14R][S][REVIEW] Independently review partition 14 report"
- BLOCKED verdict: `"blocked_on": {"referent": "card:2076c423", "value": "dependency"}`
- Dependencies list: `[]` (empty)
- Structural dependency edge: **NOT FOUND**
- Live replay output: Card **NOT** in POOL_IDS, remains in `blocked_backoff=76`

**Card ad5f9d7b Analysis:**
- Title: "[QWEN38-POOL-PREP-01][EPIC] Prepare governed two-replica Qwen3.8 pool"
- Dependencies list: `[]` (empty)
- Structural dependency edge: **NOT FOUND**
- Live replay output: Card **NOT** in POOL_IDS

**Dependency Edge Requirement (from `_blocker_change_epoch()`):**
```python
if category == "dependency":
    if len(referents) != 1: return 0
    match = _CARD_REFERENT_RE.match(referents[0])
    if not match: return 0
    dep = match.group(1).lower()
    exact_events = [event for event in event_rows(cid)
                    if event.get("action") in ("add_dependency", "remove_dependency")
                    and str(_dependency_value(event) or "").lower() == dep]
    # ... requires exact structural edge ...
```

**Result:** BOTH CARDS REMAIN PARKED WITHOUT STRUCTURAL DEPENDENCY EDGES ✓

### PASS Variants Awaiting Review

**Test `test_pass_outcomes_remain_awaiting_review`:**
```python
@pytest.mark.parametrize("value", ["PASS", "PASS_FOR_REVIEW", "PASS_FOR_REREVIEW"])
def test_pass_outcomes_remain_awaiting_review(board, value):
    card = "44444444"
    board.outcome(card, "2026-08-28T01:00:00Z", value)
    board.event(card, "2026-08-28T02:00:00Z", "amend_criteria")
    assert board.blocked(card) is True  # PASS variants stay parked
    assert board.ns["awaiting_review"](card) is True  # Correctly classified
```

**Test Results:**
- `test_pass_outcomes_remain_awaiting_review[PASS]` → PASSED ✓
- `test_pass_outcomes_remain_awaiting_review[PASS_FOR_REVIEW]` → PASSED ✓
- `test_pass_outcomes_remain_awaiting_review[PASS_FOR_REREVIEW]` → PASSED ✓

**Live replay confirmation:** `awaiting_review=13` in POOL output

**Result:** ALL PASS VARIANTS REMAIN AWAITING REVIEW ✓

---

## Acceptance Criterion 4: Regression Testing

### Focused Regression Test Results

| Command | Result |
|---------|--------|
| `python -m pytest -q tests/test_skfleet_backoff_wake.py tests/test_skfleet_dispatch_integrity.py tests/test_blocker_referent.py tests/test_blocked_verdict.py tests/test_cli_coord_deps.py` | PASS, 115 passed in 1.12s |
| `python -m py_compile scripts/fleet/skfleet-rotate.py tests/test_skfleet_backoff_wake.py` | PASS |
| `python -m ruff check tests/test_skfleet_backoff_wake.py` | PASS (All checks passed!) |
| `git diff --check` | PASS (no whitespace issues) |

### Baseline Limitation (Expected)

| Command | Result |
|---------|--------|
| `python -m ruff check --select F scripts/fleet/skfleet-rotate.py` | BASELINE LIMITATION: one unchanged F841 at existing close_reviewed_parents code (line 1319) |

**Note:** This F841 violation exists in the base commit and is not introduced by the patch.

### Specific Test Coverage

| Test | Purpose | Result |
|------|---------|--------|
| `test_exact_567_timeline_wakes_then_exhausts_one_generation` | Edge-triggered wake with claim fencing | PASSED ✓ |
| `test_dependency_requires_exact_edge_and_satisfied_referent` | Dependency blocker requires structural edge | PASSED ✓ |
| `test_exact_dependency_removal_is_a_material_change` | Dependency removal triggers wake | PASSED ✓ |
| `test_card_criterion_wakes_only_after_authored_contract_change` | Card criterion requires authored change | PASSED ✓ |
| `test_human_wake_requires_exact_explicit_approval_or_void` | Human gate requires explicit human approval | PASSED ✓ |
| `test_capability_uses_one_stronger_route_generation` | Capability routing with generation tracking | PASSED ✓ |
| `test_pass_outcomes_remain_awaiting_review` | PASS variants parked and classified correctly | PASSED ✓ |
| `test_no_change_and_unrelated_traffic_do_not_wake` | Unrelated changes do not trigger wake | PASSED ✓ |
| `test_actionable_reason_requires_one_category_and_exact_referents` | Metadata validation | PASSED ✓ |
| `test_split_blocked_on_and_referent_links_fold` | Split link folding | PASSED ✓ |
| `test_artifact_link_does_not_shadow_latest_real_outcome` | Outcome parsing robustness | PASSED ✓ |

**Result:** ALL 115 TESTS PASS ✓

---

## Code Review: Key Functions

### 1. `_blocked_reason(val)` - Metadata Validation

**Strengths:**
- Enforces exactly one category (mixed categories return `None`)
- Requires at least one referent (no bare BLOCKED)
- Supports both pipe-delimited and free-form formats
- Handles split `blocked_on` and `referent` links via `_latest_blocked_reason()`

**Verified:**
- Returns `None` for mixed categories ✓
- Returns `None` for missing referents ✓
- Correctly parses `BLOCKED|category|referent` format ✓
- Correctly parses `BLOCKED blocked_on=category referent=...` format ✓

### 2. `_wake_retry_available(cid, generation)` - Claim Fencing

**Strengths:**
- Enforces `_WAKE_RETRY_LIMIT = 1`
- Only counts launches AFTER the blocker generation
- Uses `_wake_launch_times` to track claim-fenced launches

**Verified:**
- Multiple launches within same generation exhaust retry ✓
- New BLOCKED verdict starts fresh generation ✓
- Historical launches without claim revision do not consume retry ✓

### 3. `_human_resolution_epoch(cid, referent, threshold)` - Human Gate Protection

**Strengths:**
- Requires authorized actor: `chef`, `human`, or `human-decision-recorder`
- Requires explicit `APPROVE` or `VOID` token
- Distinguishes between direct approval and gate decision
- Correctly identifies human-gate cards via label or title

**Verified:**
- Machine PASS on successor does NOT wake human-gated card ✓
- Explicit Chef/human approval DOES wake human-gated card ✓
- Void event on gate card DOES wake dependent card ✓

### 4. `_blocker_change_epoch(cid, verdict_ts, val)` - Blocker Generation Tracking

**Strengths:**
- Returns 0 (no wake) for missing/invalid metadata
- Each category has precise change detection logic
- Dependency category requires exact structural edge
- Human category requires explicit resolution events
- Capability category requires authored state change
- Card category requires complete, satisfied referents

**Verified:**
- Dependency without structural edge returns 0 ✓
- Dependency with satisfied referent returns completion epoch ✓
- Human without explicit approval returns 0 ✓
- Capability routes use stronger-model generation tracking ✓

---

## Evidence and Artifacts

### Review Worktree
```bash
Location: ~/.skcapstone/fleet/workspaces/pi-glm-chiap08-bd795bb2/review-73d5e29
Base: 73d5e294ab4b7e5d450375a983978b4e76e1107b
Patch: 426f42430643f941aede4db86b90ad2540ae6007d8a250ddfa865be9288d538e
```

### Source Evidence
```bash
Location: ~/.skcapstone/evidence/work/486eabd1/candidate/426f42430643f941aede4db86b90ad2540ae6007d8a250ddfa865be9288d538e
Files: candidate.patch, manifest.json, REVIEW-HANDOFF.md, SHA256SUMS, files/
```

### Review Evidence
```bash
Location: ~/.skcapstone/evidence/work/bd795bb2/
Files: review-report-bd795bb2.md
```

---

## Limitations and Constraints

### From Source Handoff (Verified)

1. **Missing or mixed blocker metadata:** Cards with malformed `blocked_on` remain parked and require append-only evidence repair.
2. **Dependency structural edge requirement:** Cards 83e04cf1 and ad5f9d7b remain parked until their dependency relationships are repaired with explicit structural edges.
3. **Human gate approval requirement:** A human gate wakes only from an explicit Chef, human, or human-decision-recorder approval or void event. A successor machine PASS does not discharge it.
4. **No live execution:** The candidate is source and tests only. It was not committed, pushed, merged, installed, or executed with `--go`.

### Review Scope Limitations

1. **No runtime mutation:** This review made no changes to the live fleet rotation, card store, or evidence store.
2. **No merge or push:** The candidate was not merged to main or pushed to any remote.
3. **No live rotation:** The fleet rotation was not executed with the candidate.
4. **Read-only replay:** All live board verification used read-only replay with `SKFLEET_MAX_LAUNCH=0`.

---

## Conclusion

The candidate patch `426f42430643f941aede4db86b90ad2540ae6007d8a250ddfa865be9288d538e` successfully implements exact blocker edge wake repair for the fleet rotation script. All acceptance criteria are met:

1. ✓ All source handoff hashes reproduce exactly
2. ✓ Claim-fenced retry funding is correctly enforced (one per generation)
3. ✓ Metadata validation fails closed for mixed or missing blocker information
4. ✓ Human gates are protected from machine PASS discharge
5. ✓ Cards 83e04cf1 and ad5f9d7b remain parked without structural dependency edges
6. ✓ PASS, PASS_FOR_REVIEW, and PASS_FOR_REREVIEW remain awaiting review
7. ✓ All 115 focused regression tests pass
8. ✓ The exact 567e6b09 timeline test reproduces correctly

**VERDICT: PASS**

**Evidence Hash:**
```bash
sha256sum review-report-bd795bb2.md
<to be calculated after commit>
```

**Linked to:**
- This review card: bd795bb2
- Source card: 486eabd1

---

**Reviewer:** pi-glm-chiap08-bd795bb2
**Review Completed:** 2026-08-29T01:15:00Z (estimated)
**No Source or Runtime Mutation Performed**
