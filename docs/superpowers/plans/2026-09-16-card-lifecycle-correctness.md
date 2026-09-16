# Card Lifecycle Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every dispatched card closable by the worker that claims it, and make every abandonment explicable from the ledger.

**Architecture:** Four independent defects are fixed in dependency order. A claim-event ceiling stops runaway re-dispatch using the one signal that is always written. A closed `abandon_reason` vocabulary makes stops attributable. A schema split moves other-seat obligations out of `acceptance_criteria` into `exit_gates`, and a new `awaiting-gates` ledger state lets a worker finish. Finally, cycle detection at write time stops the dependency loops that make cards permanently unclosable.

**Tech Stack:** Python 3.12, pytest, JSON-lines CardStore. Two repositories: `skcapstone` (fleet dispatcher, a standalone script) and `skcoord` (CardStore library).

**Spec:** `docs/superpowers/specs/2026-09-16-nimble-factory-design.md`

## Global Constraints

- Python 3.12. **Linting differs by repo, do not cross them over:**
  - `skcapstone`: CI runs `black --check src/ tests/` AND `ruff check src/`. Black is pinned to `26.5.1` and a `[tool.black]` section exists, so use that exact version; a different local black produces a different diff and a red build.
  - `skcoord`: CI runs ONLY `ruff check src/ tests/`. There is NO `[tool.black]` section, and `line-length = 99` lives under `[tool.ruff]`. **Never run black in skcoord**: its 88-column default reformats 66 unrelated files.
  - `skcapstone/scripts/fleet/` is outside both tools; match the file's local style instead.
- `skfleet-rotate.py` is a **script, not an importable module**. Tests extract functions from it via `ast` (see Task 1 harness). Never add an import-time side effect.
- The CardStore event ledger is **append-only**. Never rewrite or delete an event. Corrections are new events.
- Existing cards have no `spec_version`. Absent means v1 legacy behaviour and no new gate applies. Never infer v2.
- Reason strings join an existing closed vocabulary (`human-gate`, `sensitive-category`, `dependency`, `host-pin`, `non-task`, `void`, `archive`, `done`, `foreign-project`, `not-claimable`). Add to it, never repurpose an existing string.
- Never use em dashes or en dashes in any file, comment, commit message or doc. Use commas, parentheses, a colon, or a new sentence.
- Commit messages name the agent that did the work. Never add a `Co-Authored-By` you cannot evidence.
- Work in a worktree. Never branch or edit in a shared checkout.

## Spec Coverage

Every requirement in the spec maps to a task here, or is explicitly deferred to
Plan B. An executor can check this table before starting.

| Spec section | Requirement | Task |
|---|---|---|
| 3.1 | `exit_gates`, `non_goals`, `spec_version` schema | 4 |
| 3.1 | dispatch refuses non-self-satisfiable criteria | 6 |
| 3.2 | `awaiting-gates` as a ledger fact | 5 |
| 3.3 | `abandon_reason` closed vocabulary | 2 |
| 3.3 | reason required on `release_claim` | 3 |
| 3.4 | claim ceiling independent of launch evidence | 1 |
| 3.7 | cycle rejection at write time | 7 |
| 3.7 | break the 23 existing cycles | 8 |
| 4 | backfill the measured tail | 9 |
| 3.3 | callers can supply a real reason | 10 |
| 3.5 | PR policy | **deferred to Plan B** |
| 3.6 | seat topology, niobe, mero cap | **deferred to Plan B** |

## Repository Paths

Two repos are involved. Clone or worktree both before starting.

```
skcapstone   scripts/fleet/skfleet-rotate.py     dispatcher (2409 lines)
             tests/test_skfleet_backoff_wake.py  existing AST-extraction harness
skcoord      src/skcoord/card_store.py           CardStore.append_event,
                                                 add_dependency
             tests/                              pytest, fixtures/ dir present
```

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `skcapstone/scripts/fleet/skfleet-rotate.py` | claim ceiling, awaiting-gates non-claimability, criteria gate | 1, 5, 6 |
| `skcapstone/tests/test_skfleet_claim_ceiling.py` | new, ceiling tests | 1 |
| `skcapstone/tests/test_skfleet_awaiting_gates.py` | new, state tests | 5, 6 |
| `skcoord/src/skcoord/abandon_reason.py` | new, the closed vocabulary and validator | 2 |
| `skcoord/src/skcoord/card_store.py` | reason on release, cycle rejection | 3, 7 |
| `skcoord/src/skcoord/dependency_graph.py` | new, cycle detection | 7 |
| `skcoord/tests/test_abandon_reason.py` | new | 2, 3 |
| `skcoord/tests/test_dependency_cycles.py` | new | 7 |
| `skcapstone/scripts/fleet/backfill_exit_gates.py` | new, one-shot migration | 9 |

---

### Task 1: Claim ceiling keyed on claim events

Card `06a95c23` recorded 402 claims and zero completions. `blocked_backoff()` did not stop it because `launch_attempts()` reads worker logs and the outcomes store, and that card produced zero worker logs. The ledger recorded all 402 claim events reliably. Key the ceiling on claims.

**Files:**
- Modify: `skcapstone/scripts/fleet/skfleet-rotate.py` (add constant near the `MAX_LAUNCH=` assignment; add function before `blocked_backoff`; call inside `blocked_backoff`)
- Test: `skcapstone/tests/test_skfleet_claim_ceiling.py` (create)

**Interfaces:**
- Consumes: `acts(cid) -> collections.Counter` (existing) returning action counts for a card.
- Produces: `_claim_ceiling_hit(cid) -> bool`, and constant `_MAX_CLAIMS: int`.

- [ ] **Step 1: Write the failing test**

Create `skcapstone/tests/test_skfleet_claim_ceiling.py`:

```python
"""The claim ceiling stops runaway re-dispatch using ledger claim events."""

from __future__ import annotations

import ast
import collections
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {"_claim_ceiling_hit"}
CONSTANTS = {"_MAX_CLAIMS"}


def _load_ceiling_namespace(acts_result: collections.Counter) -> dict:
    """Extract the pure ceiling seam from the script without running it."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & CONSTANTS:
                nodes.append(node)
    namespace = {"os": os, "collections": collections, "acts": lambda cid: acts_result}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert FUNCTIONS <= namespace.keys(), "ceiling seam missing from script"
    return namespace


def test_runaway_card_hits_the_ceiling():
    """402 claims and no completion is the measured 06a95c23 shape."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 402, "release_claim": 8}))
    assert ns["_claim_ceiling_hit"]("06a95c23") is True


def test_healthy_card_does_not_hit_the_ceiling():
    """5a7d31ce completed on its first claim and must stay selectable."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 1, "complete": 1}))
    assert ns["_claim_ceiling_hit"]("5a7d31ce") is False


def test_completed_card_is_never_ceilinged():
    """A card that completed is finished, regardless of how many claims it took."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 99, "complete": 1}))
    assert ns["_claim_ceiling_hit"]("noisy") is False


def test_awaiting_gates_card_is_never_ceilinged():
    """A worker that finished and is waiting on another seat is not runaway."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 99, "await_gates": 1}))
    assert ns["_claim_ceiling_hit"]("waiting") is False


def test_ceiling_boundary_is_exclusive():
    """Exactly _MAX_CLAIMS is allowed; the next claim trips it."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 5}))
    assert ns["_claim_ceiling_hit"]("edge") is False
    ns = _load_ceiling_namespace(collections.Counter({"claim": 6}))
    assert ns["_claim_ceiling_hit"]("edge") is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcapstone && python -m pytest tests/test_skfleet_claim_ceiling.py -v`
Expected: FAIL with `AssertionError: ceiling seam missing from script`

- [ ] **Step 3: Add the constant**

In `scripts/fleet/skfleet-rotate.py`, immediately after the `MAX_LAUNCH=` assignment:

```python
_MAX_CLAIMS=int(os.environ.get("SKFLEET_MAX_CLAIMS","5"))
```

- [ ] **Step 4: Add the function**

Immediately before `def blocked_backoff(cid):`:

```python
def _claim_ceiling_hit(cid):
    """True when a card has been claimed repeatedly and never finished.

    Keyed on CLAIM EVENTS, not launch evidence. blocked_backoff already caps
    relaunches, but launch_attempts() reads worker logs and the outcomes store,
    and card 06a95c23 produced ZERO worker logs across 402 claims while the
    ledger recorded every one. Evidence the failing path never writes cannot
    gate the failing path.

    A card that completed, or that finished its worker-owned criteria and is
    waiting on another seat, is never runaway no matter how many claims it took.
    """
    counts = acts(cid)
    if counts.get("complete") or counts.get("await_gates"):
        return False
    return counts.get("claim", 0) > _MAX_CLAIMS
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd skcapstone && python -m pytest tests/test_skfleet_claim_ceiling.py -v`
Expected: PASS, 5 tests

- [ ] **Step 6: Wire it into the selection gate**

In `blocked_backoff(cid)`, add as the FIRST statement of the function body, before any other check:

```python
    if _claim_ceiling_hit(cid):
        return True
```

- [ ] **Step 7: Verify the existing backoff suite still passes**

Run: `cd skcapstone && python -m pytest tests/test_skfleet_backoff_wake.py -v`
Expected: PASS, no regressions. If `_load_backoff_namespace` fails with a NameError for `_claim_ceiling_hit`, add `"_claim_ceiling_hit"` to that file's `FUNCTIONS` set and `"_MAX_CLAIMS"` to its `CONSTANTS` set.

- [ ] **Step 8: Commit**

```bash
git add tests/test_skfleet_claim_ceiling.py scripts/fleet/skfleet-rotate.py
git commit -m "fix(fleet): cap re-dispatch on claim events, not launch evidence

Card 06a95c23 recorded 402 claims and zero completions. blocked_backoff
did not stop it because launch_attempts reads worker logs and the
outcomes store, and that card produced zero worker logs. The ledger
recorded all 402 claim events. Key the ceiling on the signal that is
always written.

Completed and awaiting-gates cards are exempt: they are finished, not
runaway."
```

---

### Task 2: The abandon_reason vocabulary

53 percent of open SKLegal cards were abandoned with no recorded cause. A closed vocabulary makes the ledger answer the question.

**Files:**
- Create: `skcoord/src/skcoord/abandon_reason.py`
- Test: `skcoord/tests/test_abandon_reason.py` (create)

**Interfaces:**
- Produces: `ABANDON_REASONS: frozenset[str]`, and `validate_abandon_reason(value: str | None) -> str` which returns the normalised reason or raises `ValueError`.

- [ ] **Step 1: Write the failing test**

Create `skcoord/tests/test_abandon_reason.py`:

```python
"""The abandon_reason vocabulary is closed and normalising."""

from __future__ import annotations

import pytest

from skcoord.abandon_reason import ABANDON_REASONS, validate_abandon_reason


def test_vocabulary_is_exactly_the_specified_reasons():
    assert ABANDON_REASONS == frozenset(
        {
            "criteria-unsatisfiable",
            "dependency-unsatisfied",
            "capability-missing",
            "error",
            "superseded",
            "unspecified",
        }
    )


@pytest.mark.parametrize("reason", sorted(ABANDON_REASONS))
def test_every_valid_reason_round_trips(reason):
    assert validate_abandon_reason(reason) == reason


def test_case_and_whitespace_are_normalised():
    assert validate_abandon_reason("  Criteria-Unsatisfiable  ") == "criteria-unsatisfiable"


def test_unknown_reason_is_rejected_with_the_vocabulary_in_the_message():
    with pytest.raises(ValueError) as excinfo:
        validate_abandon_reason("because-i-felt-like-it")
    message = str(excinfo.value)
    assert "because-i-felt-like-it" in message
    assert "criteria-unsatisfiable" in message


def test_missing_reason_defaults_to_unspecified_and_never_raises():
    """Reaper and sweep paths must always succeed, even without a reason."""
    assert validate_abandon_reason(None) == "unspecified"
    assert validate_abandon_reason("") == "unspecified"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcoord && python -m pytest tests/test_abandon_reason.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skcoord.abandon_reason'`

- [ ] **Step 3: Write the implementation**

Create `skcoord/src/skcoord/abandon_reason.py`:

```python
"""The closed vocabulary for why a worker stopped.

Measured 2026-09-16: 235 of 444 open SKLegal cards (53 percent) had been
claimed and abandoned with no recorded cause. The ledger faithfully recorded
THAT work stopped and never WHY, so more than half the residue was
unattributable. A factory cannot improve what it does not write down.

The vocabulary is deliberately closed and deliberately small. An open text
field would reproduce the current situation with extra steps.
"""

from __future__ import annotations

ABANDON_REASONS = frozenset(
    {
        # The worker cannot satisfy a stated acceptance criterion.
        "criteria-unsatisfiable",
        # A declared dependency is not met.
        "dependency-unsatisfied",
        # The worker lacks a tool, credential, or host-local asset.
        "capability-missing",
        # The worker failed. The message belongs in the event payload.
        "error",
        # Another worker or a human took the work.
        "superseded",
        # The caller did not say. Explicit sentinel, NOT a silent default.
        # Reaper and stale-claim paths must always succeed and often cannot know
        # why a worker stopped, so a path that must not fail needs a
        # representable answer. Measuring the share of this value is how we know
        # the migration is working.
        "unspecified",
    }
)


def validate_abandon_reason(value: str | None) -> str:
    """Return the normalised reason. Absent becomes "unspecified", never raises.

    An UNKNOWN non-empty value still raises, because that is a caller bug worth
    surfacing. An ABSENT value does not, because several dispatcher release paths
    (reapers, stale-claim sweeps) must always succeed. Making those raise would
    strand the claims they exist to free, which is worse than the defect this
    module exists to fix.
    """
    if value is None or not str(value).strip():
        return "unspecified"
    normalised = str(value).strip().lower()
    if normalised not in ABANDON_REASONS:
        raise ValueError(
            f"abandon_reason {value!r} is not in the closed vocabulary: "
            + ", ".join(sorted(ABANDON_REASONS))
        )
    return normalised
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd skcoord && python -m pytest tests/test_abandon_reason.py -v`
Expected: PASS, 9 tests (5 parametrised plus 4)

- [ ] **Step 5: Format and lint**

Run: `cd skcoord && ruff check src/ tests/`
Expected: no findings.

Do NOT run `black`. This repo has no `[tool.black]` section, its `line-length = 99` is under `[tool.ruff]`, and CI runs only `ruff check src/ tests/`. Running black reformats 66 unrelated files.

- [ ] **Step 6: Commit**

```bash
git add src/skcoord/abandon_reason.py tests/test_abandon_reason.py
git commit -m "feat(cardstore): closed vocabulary for why a worker stopped

Measured 2026-09-16: 235 of 444 open SKLegal cards were claimed and
abandoned with no recorded cause, so 53 percent of the residue was
unattributable. The ledger recorded that work stopped and never why.

The vocabulary is closed on purpose. An open text field reproduces the
current situation with extra steps."
```

---

### Task 3: Require abandon_reason on release_claim

**Files:**
- Modify: `skcoord/src/skcoord/card_store.py` (`append_event`)
- Test: `skcoord/tests/test_abandon_reason.py` (extend)

**Interfaces:**
- Consumes: `validate_abandon_reason(value) -> str` from Task 2.
- Consumes: `CardStore.append_event(card_id, action, agent, **payload) -> dict` (existing).
- Produces: no new symbol. `append_event` now raises `ValueError` for a `release_claim` without a valid reason.

- [ ] **Step 1: Write the failing test**

Append to `skcoord/tests/test_abandon_reason.py`:

```python
def test_release_claim_without_a_reason_records_unspecified(tmp_path):
    """MUST NOT raise. Dispatcher reaper paths supply no reason and must succeed."""
    from skcoord.card_store import CardStore

    store = CardStore(tmp_path)
    store.create(title="probe card", kind="task", agent="tester")
    card_id = sorted(p.name for p in (tmp_path / "cards").iterdir())[0]

    event = store.append_event(card_id, "release_claim", "worker-1")
    assert event["abandon_reason"] == "unspecified"


def test_release_claim_with_a_valid_reason_is_recorded(tmp_path):
    from skcoord.card_store import CardStore

    store = CardStore(tmp_path)
    store.create(title="probe card", kind="task", agent="tester")
    card_id = sorted(p.name for p in (tmp_path / "cards").iterdir())[0]

    event = store.append_event(
        card_id, "release_claim", "worker-1", abandon_reason="  Dependency-Unsatisfied "
    )
    assert event["abandon_reason"] == "dependency-unsatisfied"


def test_other_actions_do_not_require_a_reason(tmp_path):
    """Only release_claim is constrained. claim and complete are unaffected."""
    from skcoord.card_store import CardStore

    store = CardStore(tmp_path)
    store.create(title="probe card", kind="task", agent="tester")
    card_id = sorted(p.name for p in (tmp_path / "cards").iterdir())[0]

    assert store.append_event(card_id, "claim", "worker-1")
    assert store.append_event(card_id, "complete", "worker-1")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcoord && python -m pytest tests/test_abandon_reason.py -k release_claim -v`
Expected: FAIL with `KeyError: 'abandon_reason'`, because the event carries no such field yet.

- [ ] **Step 3: Write the implementation**

In `skcoord/src/skcoord/card_store.py`, add the import at the top of the file with the other local imports:

```python
from .abandon_reason import validate_abandon_reason
```

Then in `append_event`, immediately after the existing `validate_card_lock_identifier(card_id)` line:

```python
        if action == "release_claim":
            # Every stop must be attributable. Without this the ledger records
            # THAT a worker gave up and never WHY, which left 53 percent of the
            # open residue unexplainable when measured on 2026-09-16.
            #
            # This NORMALISES, it does not reject. Several dispatcher call sites
            # release claims today with no reason, and a reaper path that cannot
            # release is worse than one that releases without saying why.
            # Enforcement tightens only after Task 10 raises reason coverage.
            payload["abandon_reason"] = validate_abandon_reason(
                payload.get("abandon_reason")
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd skcoord && python -m pytest tests/test_abandon_reason.py -v`
Expected: PASS, all tests

- [ ] **Step 5: Run the full CardStore suite for regressions**

Run: `cd skcoord && python -m pytest tests/ -q`
Expected: PASS with no changes to existing tests. Because absence normalises rather than raising, no existing caller breaks. If any test DOES fail, stop: that means the normalising path is raising somewhere it must not.

- [ ] **Step 6: Commit**

```bash
git add src/skcoord/card_store.py tests/test_abandon_reason.py
git commit -m "feat(cardstore): record abandon_reason on release_claim

Closes the 53 percent unattributable residue measured 2026-09-16.

Normalises rather than rejects. the worker launch string wraps every
worker in a shell trap releasing on EXIT/HUP/INT/TERM with no reason;
rejecting would strand every claim that trap failed to release. Absent
becomes the explicit sentinel unspecified, whose share is the migration
metric."
```

---

### Task 4: Schema fields for the worker/other-seat split

**Files:**
- Modify: `skcoord/src/skcoord/card_store.py` (the `CardCore` pydantic model, and `abandon_reason.py` for the validator)
- Test: `skcoord/tests/test_exit_gates_schema.py` (create)

**Interfaces:**
- `CardCore` is a **pydantic BaseModel** defined in `card_store.py`, NOT a dataclass, and `CardStore.create` takes it as a single positional argument: `def create(self, core: CardCore) -> str`.
- Existing call pattern, copy it: `store.create(CardCore(id="probe01", title="probe card"))`.
- Produces: `CardCore` gains three optional fields, `exit_gates: list[dict] = []`, `non_goals: list[str] = []`, `spec_version: int | None = None`. Absent `spec_version` means v1.
- Produces: `validate_exit_gates(gates: object) -> list[dict]` in `abandon_reason.py`.

- [ ] **Step 1: Write the failing test**

Create `skcoord/tests/test_exit_gates_schema.py`:

```python
"""exit_gates, non_goals and spec_version round-trip through CardCore."""

from __future__ import annotations

import json

import pytest

from skcoord.card_store import CardCore, CardStore


def _core_json(tmp_path, card_id):
    return json.loads((tmp_path / "cards" / card_id / "core.json").read_text())


def test_legacy_card_has_no_spec_version(tmp_path):
    """Absent means v1. Never infer v2."""
    store = CardStore(tmp_path)
    card_id = store.create(CardCore(id="legacy01", title="legacy"))
    core = _core_json(tmp_path, card_id)
    assert core.get("spec_version") in (None, 1)


def test_exit_gates_round_trip(tmp_path):
    store = CardStore(tmp_path)
    gates = [{"gate": "independent-review", "owner": "seraph", "ref": "parent-5a7e5f41"}]
    card_id = store.create(
        CardCore(
            id="v2card01",
            title="v2 card",
            exit_gates=gates,
            non_goals=["no deployment"],
            spec_version=2,
        )
    )
    core = _core_json(tmp_path, card_id)
    assert core["exit_gates"] == gates
    assert core["non_goals"] == ["no deployment"]
    assert core["spec_version"] == 2


def test_prose_exit_gate_is_rejected(tmp_path):
    """A prose string cannot be checked mechanically, so it is rejected."""
    from skcoord.abandon_reason import validate_exit_gates

    with pytest.raises(ValueError):
        validate_exit_gates(["independent review PASS before merge"])


def test_exit_gate_without_owner_is_rejected():
    from skcoord.abandon_reason import validate_exit_gates

    with pytest.raises(ValueError):
        validate_exit_gates([{"gate": "independent-review"}])


def test_valid_exit_gate_passes_validation():
    from skcoord.abandon_reason import validate_exit_gates

    gates = [{"gate": "independent-review", "owner": "seraph"}]
    assert validate_exit_gates(gates) == gates


def test_none_exit_gates_is_an_empty_list():
    from skcoord.abandon_reason import validate_exit_gates

    assert validate_exit_gates(None) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcoord && python3 -m pytest tests/test_exit_gates_schema.py -v`
Expected: FAIL. `validate_exit_gates` does not exist, and `CardCore` rejects the unknown keyword arguments.

- [ ] **Step 3: Write the validator**

Add to `skcoord/src/skcoord/abandon_reason.py`:

```python
def validate_exit_gates(gates: object) -> list[dict]:
    """Return the gate list, or raise ValueError.

    Entries are objects, never prose. The dispatcher needs `owner` to route the
    gate to a seat and `gate` to name it. A string such as "independent review
    PASS before merge" cannot be checked mechanically, which is exactly the
    defect that let card 06a95c23 accumulate 402 claims.
    """
    if gates is None:
        return []
    if not isinstance(gates, list):
        raise ValueError("exit_gates must be a list of objects")
    validated = []
    for entry in gates:
        if not isinstance(entry, dict):
            raise ValueError(f"exit_gates entry must be an object, got {entry!r}")
        if not str(entry.get("gate") or "").strip():
            raise ValueError(f"exit_gates entry needs a 'gate' name: {entry!r}")
        if not str(entry.get("owner") or "").strip():
            raise ValueError(f"exit_gates entry needs an 'owner' seat: {entry!r}")
        validated.append(dict(entry))
    return validated
```

- [ ] **Step 4: Add the fields to CardCore**

In `card_store.py`, on the `CardCore` pydantic model, add three optional fields
alongside the existing ones. Match the model's existing field style:

```python
    exit_gates: list[dict] = []
    non_goals: list[str] = []
    spec_version: int | None = None
```

If the model uses pydantic validators for other fields, add one for `exit_gates`
that calls `validate_exit_gates`, so a bad gate is rejected at construction. If it
does not use validators, leave the function available for callers and note that in
your report.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd skcoord && python3 -m pytest tests/test_exit_gates_schema.py -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Run the full suite**

Run: `cd skcoord && python3 -m pytest tests/ -q`
Expected: PASS. Adding optional fields with defaults must not change any existing
card's serialisation. If an existing test asserts an exact `core.json` key set,
that is a real finding: report it rather than editing the assertion.

- [ ] **Step 7: Lint**

Run: `cd skcoord && ruff check src/ tests/`
Expected: no findings. Do NOT run `black`.

- [ ] **Step 8: Commit**

```bash
git add src/skcoord/card_store.py src/skcoord/abandon_reason.py tests/test_exit_gates_schema.py
git commit -m "feat(cardstore): exit_gates, non_goals and spec_version on CardCore

Separates what the worker owns from what another seat owns. exit_gates
entries are objects because the dispatcher needs owner to route them; a
prose gate cannot be checked, which is how card 06a95c23 reached 402
claims against a criterion belonging to a reviewer.

spec_version absent means v1 legacy, so adoption is per-card and
reversible rather than a flag day across 7,147 cards."
```

---

### Task 5: The awaiting-gates state

**Files:**
- Modify: `skcapstone/scripts/fleet/skfleet-rotate.py` (`_fold_claimability`, `_claimability_reason`)
- Test: `skcapstone/tests/test_skfleet_awaiting_gates.py` (create)

**Interfaces:**
- Consumes: `_fold_claimability(core, rows) -> dict` (existing) whose returned dict carries `status`, `owner`, `labels`, `dependencies`, `voided`, `archived`, `title`.
- Produces: folded state gains key `awaiting_gates: bool`. `_claimability_reason` returns the new string `"awaiting-gates"`.

- [ ] **Step 1: Write the failing test**

Create `skcapstone/tests/test_skfleet_awaiting_gates.py`:

```python
"""A worker that satisfied its criteria is finished, not re-dispatchable."""

from __future__ import annotations

import ast
import collections
import datetime
import glob
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(names, constants):
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            ids = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if ids & constants:
                nodes.append(node)
    ns = {
        "collections": collections,
        "datetime": datetime,
        "glob": glob,
        "json": json,
        "os": os,
        "re": re,
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), ns)
    return ns


def test_await_gates_event_sets_the_folded_flag():
    ns = _load({"_fold_claimability"}, set())
    core = {"title": "t", "dependencies": [], "initial_labels": []}
    rows = [{"action": "claim", "owner": "w1"}, {"action": "await_gates"}]
    state = ns["_fold_claimability"](core, rows)
    assert state["awaiting_gates"] is True


def test_no_await_gates_event_leaves_the_flag_false():
    ns = _load({"_fold_claimability"}, set())
    core = {"title": "t", "dependencies": [], "initial_labels": []}
    state = ns["_fold_claimability"](core, [{"action": "claim", "owner": "w1"}])
    assert state["awaiting_gates"] is False


def test_reopen_clears_awaiting_gates():
    """A reopened card is workable again; the flag must not be sticky."""
    ns = _load({"_fold_claimability"}, set())
    core = {"title": "t", "dependencies": [], "initial_labels": []}
    rows = [{"action": "await_gates"}, {"action": "reopen"}]
    state = ns["_fold_claimability"](core, rows)
    assert state["awaiting_gates"] is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcapstone && python -m pytest tests/test_skfleet_awaiting_gates.py -v`
Expected: FAIL with `KeyError: 'awaiting_gates'`

- [ ] **Step 3: Fold the new action**

In `_fold_claimability`, add `"awaiting_gates": False,` to the initial `state` dict (alongside the existing `"dependencies"` key at L507). Then in the action loop, alongside the existing `elif action == "release_claim":` branch in the action loop:

```python
        elif action == "await_gates":
            state["awaiting_gates"] = True
        elif action == "reopen":
            state["awaiting_gates"] = False
```

If a `reopen` branch already exists, add the `awaiting_gates` reset into it rather than adding a second branch.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd skcapstone && python -m pytest tests/test_skfleet_awaiting_gates.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Make awaiting-gates non-claimable**

In `_claimability_reason`, add immediately after the `if state["status"] == "done": return "done"` line:

```python
    if state.get("awaiting_gates"):
        return "awaiting-gates"
```

- [ ] **Step 6: Add the reason test**

Append to `tests/test_skfleet_awaiting_gates.py`:

```python
def test_awaiting_gates_card_is_not_claimable():
    ns = _load({"_claimability_reason", "_coord_task_claimable", "non_implementation", "host_pin"}, set())
    state = {
        "title": "t",
        "labels": [],
        "dependencies": [],
        "owner": "",
        "status": "doing",
        "voided": False,
        "archived": False,
        "awaiting_gates": True,
    }
    assert ns["_claimability_reason"]({"kind": "task"}, state) == "awaiting-gates"
```

If this test fails on a missing helper name, add that helper to the `_load` names set. Do not stub it.

- [ ] **Step 7: Run both tests and the existing claimability suite**

Run: `cd skcapstone && python -m pytest tests/test_skfleet_awaiting_gates.py tests/test_skfleet_claimability.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add tests/test_skfleet_awaiting_gates.py scripts/fleet/skfleet-rotate.py
git commit -m "feat(fleet): awaiting-gates is a ledger fact, not an inference

awaiting_review() infers the state by regex-matching a PASS value in the
outcomes store, which the failing path may never write. await_gates is a
card event, so a worker that satisfied its own criteria is recorded as
finished and stops being re-dispatched."
```

---

### Task 6: Refuse cards whose criteria the worker cannot satisfy

Applies only to `spec_version >= 2`. Legacy cards are untouched.

**Files:**
- Modify: `skcapstone/scripts/fleet/skfleet-rotate.py` (`_claimability_reason`)
- Test: `skcapstone/tests/test_skfleet_awaiting_gates.py` (extend)

**Interfaces:**
- Produces: `_GATE_LANGUAGE_RE: re.Pattern`, and `_claimability_reason` returns `"criteria-not-satisfiable"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_skfleet_awaiting_gates.py`:

```python
BASE_STATE = {
    "title": "t",
    "labels": [],
    "dependencies": [],
    "owner": "",
    "status": "ready",
    "voided": False,
    "archived": False,
    "awaiting_gates": False,
}
HELPERS = {"_claimability_reason", "_coord_task_claimable", "non_implementation", "host_pin"}


def test_v2_card_with_reviewer_criterion_is_refused():
    """The measured 06a95c23 criterion: AC4 named a reviewer's verdict."""
    ns = _load(HELPERS, {"_GATE_LANGUAGE_RE"})
    core = {
        "kind": "task",
        "spec_version": 2,
        "acceptance_criteria": [
            "Independent review PASS on the successor commit before merge"
        ],
    }
    assert ns["_claimability_reason"](core, dict(BASE_STATE)) == "criteria-not-satisfiable"


def test_v2_card_with_self_satisfiable_criteria_is_claimable():
    """5a7d31ce's shape: every criterion falsifiable by a test."""
    ns = _load(HELPERS, {"_GATE_LANGUAGE_RE"})
    core = {
        "kind": "task",
        "spec_version": 2,
        "acceptance_criteria": [
            "Add a deterministic fixture reproducing e125b710",
            "Pass focused and full scheduler tests, Ruff, formatting and compile",
        ],
    }
    assert ns["_claimability_reason"](core, dict(BASE_STATE)) == "claimable"


def test_legacy_card_with_reviewer_criterion_is_untouched():
    """spec_version absent means v1. The new gate must not apply."""
    ns = _load(HELPERS, {"_GATE_LANGUAGE_RE"})
    core = {
        "kind": "task",
        "acceptance_criteria": [
            "Independent review PASS on the successor commit before merge"
        ],
    }
    assert ns["_claimability_reason"](core, dict(BASE_STATE)) == "claimable"


def test_gate_language_in_exit_gates_is_fine():
    """Gate language belongs in exit_gates. Only acceptance_criteria is checked."""
    ns = _load(HELPERS, {"_GATE_LANGUAGE_RE"})
    core = {
        "kind": "task",
        "spec_version": 2,
        "acceptance_criteria": ["Tests pass"],
        "exit_gates": [{"gate": "independent-review", "owner": "seraph"}],
    }
    assert ns["_claimability_reason"](core, dict(BASE_STATE)) == "claimable"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcapstone && python -m pytest tests/test_skfleet_awaiting_gates.py -k criteria -v`
Expected: FAIL. The reviewer-criterion test returns `"claimable"` instead of `"criteria-not-satisfiable"`.

- [ ] **Step 3: Add the pattern**

In `skfleet-rotate.py`, immediately after the `_SENSITIVE_CATEGORY` assignment:

```python
# Criteria a worker CANNOT satisfy alone: they name another seat's verdict or a
# merge. Measured 2026-09-16: 160 of 444 open SKLegal cards carried one, and
# those cards averaged 3.39 claims against 1.96 for cards without.
_GATE_LANGUAGE_RE = re.compile(
    r"independent review|reviewer|review pass|before merge|approval|approved by"
    r"|sign-?off|merged",
    re.I,
)
```

- [ ] **Step 4: Add the branch**

In `_claimability_reason`, immediately after the `awaiting-gates` branch from Task 5:

```python
    if int(core.get("spec_version") or 1) >= 2:
        criteria = " ".join(str(c) for c in (core.get("acceptance_criteria") or []))
        if _GATE_LANGUAGE_RE.search(criteria):
            return "criteria-not-satisfiable"
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd skcapstone && python -m pytest tests/test_skfleet_awaiting_gates.py -v`
Expected: PASS, 8 tests

- [ ] **Step 6: Commit**

```bash
git add tests/test_skfleet_awaiting_gates.py scripts/fleet/skfleet-rotate.py
git commit -m "feat(fleet): refuse v2 cards whose criteria name another seat

Measured 2026-09-16: 160 of 444 open SKLegal cards carry a criterion the
worker cannot satisfy, and those average 3.39 claims against 1.96 for
cards without. Gate language belongs in exit_gates.

Applies only to spec_version 2 and above. Legacy cards are untouched."
```

---

### Task 7: Reject dependency edges that would create a cycle

23 cycles exist, all one pathology: `SKLEGAL-SECRET-COHORT-SLICE-R1` leaves and their parents depend on each other, making 35 cards permanently unclosable.

**Files:**
- Create: `skcoord/src/skcoord/dependency_graph.py`
- Modify: `skcoord/src/skcoord/card_store.py` (`amend_dependency`, reached via `add_dependency`)
- Test: `skcoord/tests/test_dependency_cycles.py` (create)

**Interfaces:**
- Produces: `would_create_cycle(edges: dict[str, list[str]], card_id: str, dependency_id: str) -> bool`.
- `add_dependency(home, card_id, dependency_id, agent="", reason="") -> bool` now raises `ValueError` when the edge would close a cycle.

- [ ] **Step 1: Write the failing test**

Create `skcoord/tests/test_dependency_cycles.py`:

```python
"""Cycle detection at write time, not discovery time."""

from __future__ import annotations

import pytest

from skcoord.dependency_graph import would_create_cycle


def test_self_dependency_is_a_cycle():
    assert would_create_cycle({}, "a", "a") is True


def test_direct_two_card_cycle():
    """The measured pathology: parent depends on leaf, leaf on parent."""
    edges = {"leaf": ["parent"]}
    assert would_create_cycle(edges, "parent", "leaf") is True


def test_transitive_cycle():
    edges = {"b": ["c"], "c": ["a"]}
    assert would_create_cycle(edges, "a", "b") is True


def test_diamond_is_not_a_cycle():
    """Two paths to a shared dependency are legal."""
    edges = {"b": ["d"], "c": ["d"]}
    assert would_create_cycle(edges, "a", "b") is False
    assert would_create_cycle(edges, "a", "c") is False


def test_unrelated_edge_is_not_a_cycle():
    assert would_create_cycle({"x": ["y"]}, "a", "b") is False


def test_add_dependency_rejects_a_cycle(tmp_path):
    from skcoord.card_store import CardStore, add_dependency

    store = CardStore(tmp_path)
    store.create(title="parent", kind="task", agent="tester")
    store.create(title="leaf", kind="task", agent="tester")
    parent, leaf = sorted(p.name for p in (tmp_path / "cards").iterdir())

    assert add_dependency(tmp_path, leaf, parent, agent="tester") is True
    with pytest.raises(ValueError) as excinfo:
        add_dependency(tmp_path, parent, leaf, agent="tester")
    assert "cycle" in str(excinfo.value).lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcoord && python -m pytest tests/test_dependency_cycles.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skcoord.dependency_graph'`

- [ ] **Step 3: Write the detector**

Create `skcoord/src/skcoord/dependency_graph.py`:

```python
"""Dependency cycle detection for the coordination card graph.

Measured 2026-09-16: 23 cycles existed, essentially one pathology. The
SKLEGAL-SECRET-COHORT-SLICE-R1 leaves and their parent "Resolve or quarantine"
cards depended on each other, so 35 open cards could never reach completion and
6 of the 11 worst claim-thrashers were cycle-blocked. Workers claimed, found the
dependency unsatisfiable, released, and repeated.

Detection belongs at write time. The graph is small and the check is cheap, so
there is no reason to discover this later.
"""

from __future__ import annotations


def would_create_cycle(
    edges: dict[str, list[str]], card_id: str, dependency_id: str
) -> bool:
    """True if card_id depending on dependency_id closes a cycle.

    ``edges`` maps a card to the cards it already depends on. The new edge is
    card_id -> dependency_id, so a cycle exists when dependency_id can already
    reach card_id.
    """
    if card_id == dependency_id:
        return True
    seen = set()
    stack = [dependency_id]
    while stack:
        current = stack.pop()
        if current == card_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edges.get(current, ()))
    return False
```

- [ ] **Step 4: Run the detector tests**

Run: `cd skcoord && python -m pytest tests/test_dependency_cycles.py -k "not add_dependency" -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Wire it into amend_dependency**

In `card_store.py`, import the detector and guard the add path. Inside `amend_dependency`, before appending the event, when `action == "add_dependency"`:

```python
    if action == "add_dependency":
        edges = {
            path.name: list(
                (json.loads((path / "core.json").read_text()).get("dependencies") or [])
            )
            for path in (Path(home) / "cards").iterdir()
            if (path / "core.json").exists()
        }
        if would_create_cycle(edges, card_id, dependency_id):
            raise ValueError(
                f"dependency {card_id} -> {dependency_id} would create a cycle"
            )
```

Note: `edges` must reflect FOLDED dependencies, not only `core.json`. If the repository already exposes a folded-dependency helper, use it here instead of reading `core.json` directly, and state which helper you used in the commit message.

- [ ] **Step 6: Run the full cycle suite**

Run: `cd skcoord && python -m pytest tests/test_dependency_cycles.py -v`
Expected: PASS, 6 tests

- [ ] **Step 7: Run the full CardStore suite**

Run: `cd skcoord && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/skcoord/dependency_graph.py src/skcoord/card_store.py tests/test_dependency_cycles.py
git commit -m "feat(cardstore): reject dependency edges that close a cycle

Measured 2026-09-16: 23 cycles existed, all one pathology. SECRET-COHORT
leaves and their parents depended on each other, so 35 open cards could
never complete and 6 of the 11 worst claim-thrashers were cycle-blocked.

The graph is small and the check is cheap. Detection belongs at write
time, not discovery time."
```

---

### Task 8: Break the 23 existing cycles

Task 7 prevents new cycles. The 23 already in the store still block 35 cards.

**Files:**
- Create: `skcapstone/scripts/fleet/break_dependency_cycles.py`
- Test: `skcapstone/tests/test_break_dependency_cycles.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks. Task 7's `would_create_cycle` answers "would this ONE edge close a cycle", which is the write-time question. This task needs to ENUMERATE existing cycles, which is a different algorithm, so it implements its own.
- Consumes: `remove_dependency(home, card_id, dependency_id, agent="", reason="") -> bool` from `skcoord.card_store` (existing).
- Produces: `find_cycles(edges: dict[str, list[str]]) -> list[list[str]]`, and `plan_breaks(cycles: list[list[str]], parents: dict[str, str]) -> list[tuple[str, str]]` returning the `(card_id, dependency_id)` edges to remove.

- [ ] **Step 1: Write the failing test**

Create `skcapstone/tests/test_break_dependency_cycles.py`:

```python
"""Cycle breaking removes the parent-to-child edge, never the child-to-parent."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from break_dependency_cycles import find_cycles, plan_breaks  # noqa: E402


def test_find_cycles_detects_the_two_card_pathology():
    cycles = find_cycles({"leaf": ["parent"], "parent": ["leaf"]})
    assert len(cycles) == 1
    assert set(cycles[0]) == {"leaf", "parent"}


def test_find_cycles_ignores_a_clean_graph():
    assert find_cycles({"leaf": ["parent"], "parent": []}) == []


def test_plan_breaks_removes_the_parent_to_child_edge():
    """A parent must not depend on its own child. That is what closes the loop."""
    cycles = [["parent", "leaf"]]
    parents = {"leaf": "parent"}
    assert plan_breaks(cycles, parents) == [("parent", "leaf")]


def test_plan_breaks_leaves_the_child_to_parent_edge_intact():
    cycles = [["parent", "leaf"]]
    parents = {"leaf": "parent"}
    breaks = plan_breaks(cycles, parents)
    assert ("leaf", "parent") not in breaks
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcapstone && python -m pytest tests/test_break_dependency_cycles.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'break_dependency_cycles'`

- [ ] **Step 3: Write the script**

Create `skcapstone/scripts/fleet/break_dependency_cycles.py`:

```python
#!/usr/bin/env python3
"""Find and break the dependency cycles that make cards permanently unclosable.

Measured 2026-09-16: 23 cycles, essentially one pathology. A parent "Resolve or
quarantine" card depended on its own leaves while the leaves depended on it.

The break is always the PARENT-to-CHILD edge. A child legitimately depends on
its parent's outcome; a parent depending on its own child is what closes the
loop. Removing the wrong edge would orphan the child instead of freeing it.

Dry run by default. Pass --apply to write remove_dependency events.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def find_cycles(edges: dict[str, list[str]]) -> list[list[str]]:
    """Return one representative node list per distinct cycle."""
    cycles: list[list[str]] = []
    seen_signatures: set[frozenset[str]] = set()
    colour: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        colour[node] = 1
        path.append(node)
        for nxt in edges.get(node, ()):
            if colour.get(nxt) == 1:
                cycle = path[path.index(nxt) :]
                signature = frozenset(cycle)
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    cycles.append(list(cycle))
            elif colour.get(nxt, 0) == 0:
                visit(nxt, path)
        path.pop()
        colour[node] = 2

    for node in list(edges):
        if colour.get(node, 0) == 0:
            visit(node, [])
    return cycles


def plan_breaks(
    cycles: list[list[str]], parents: dict[str, str]
) -> list[tuple[str, str]]:
    """Return the (card_id, dependency_id) edges to remove."""
    breaks: list[tuple[str, str]] = []
    for cycle in cycles:
        members = set(cycle)
        for child, parent in parents.items():
            if child in members and parent in members:
                edge = (parent, child)
                if edge not in breaks:
                    breaks.append(edge)
    return breaks


def load_graph(home: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Read the dependency graph and the parent map from the CardStore."""
    edges: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    for card_dir in (home / "cards").iterdir():
        core_path = card_dir / "core.json"
        if not core_path.exists():
            continue
        core = json.loads(core_path.read_text(encoding="utf-8"))
        edges[card_dir.name] = [str(x) for x in (core.get("dependencies") or [])]
        title = str(core.get("title") or "")
        if "parent-" in title:
            marker = title.split("parent-", 1)[1]
            parents[card_dir.name] = marker.split()[0].strip("]").strip()
    return edges, parents


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(Path.home() / ".skcapstone"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    home = Path(args.home)
    edges, parents = load_graph(home)
    cycles = find_cycles(edges)
    breaks = plan_breaks(cycles, parents)

    print(f"cycles found: {len(cycles)}")
    print(f"edges to remove: {len(breaks)}")
    for parent, child in breaks:
        print(f"  remove {parent} -> {child}")

    unbroken = [c for c in cycles if not any(set(c) & set(b) for b in breaks)]
    if unbroken:
        print(f"WARNING {len(unbroken)} cycles have no parent edge to break:")
        for cycle in unbroken:
            print(f"  {' -> '.join(cycle)}")

    if not args.apply:
        print("dry run. pass --apply to write remove_dependency events")
        return 0

    from skcoord.card_store import remove_dependency

    for parent, child in breaks:
        remove_dependency(
            home, parent, child, agent="cycle-breaker", reason="dependency-cycle"
        )
        print(f"  removed {parent} -> {child}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd skcapstone && python -m pytest tests/test_break_dependency_cycles.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Dry run against the real store**

Run: `ssh skuser01@chiap08 'cd ~/work/skcapstone && ~/.skenv/bin/python scripts/fleet/break_dependency_cycles.py --home ~/.skcapstone'`
Expected: reports roughly 23 cycles. Read the `WARNING` block. If any cycle has no parent edge, STOP and report it rather than applying; those need a human decision about which edge is wrong.

- [ ] **Step 6: Apply**

Run: `ssh skuser01@chiap08 'cd ~/work/skcapstone && ~/.skenv/bin/python scripts/fleet/break_dependency_cycles.py --home ~/.skcapstone --apply'`
Expected: each removal printed.

- [ ] **Step 7: Verify zero cycles remain**

Run: `ssh skuser01@chiap08 'cd ~/work/skcapstone && ~/.skenv/bin/python scripts/fleet/break_dependency_cycles.py --home ~/.skcapstone'`
Expected: `cycles found: 0`

- [ ] **Step 8: Commit**

```bash
git add scripts/fleet/break_dependency_cycles.py tests/test_break_dependency_cycles.py
git commit -m "feat(fleet): break the 23 existing dependency cycles

Task 7 stops new cycles. These 23 predate it and block 35 open cards.

The break is always the parent-to-child edge. A child legitimately
depends on its parent's outcome; a parent depending on its own child is
what closes the loop. Dry run by default."
```

---

### Task 9: Backfill exit_gates on the measured tail

Only cards still open AND claimed 3 or more times. Not all 160, and never a closed card.

**Files:**
- Create: `skcapstone/scripts/fleet/backfill_exit_gates.py`
- Test: `skcapstone/tests/test_backfill_exit_gates.py` (create)

**Interfaces:**
- Consumes: `_GATE_LANGUAGE_RE` semantics from Task 6 (re-declared locally; the script must not import from `skfleet-rotate.py`, which is not importable).
- Produces: `split_criteria(criteria: list[str]) -> tuple[list[str], list[dict]]` returning worker-owned criteria and derived gates.

- [ ] **Step 1: Write the failing test**

Create `skcapstone/tests/test_backfill_exit_gates.py`:

```python
"""Backfill splits criteria; it never rewrites or drops one."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from backfill_exit_gates import split_criteria  # noqa: E402


def test_reviewer_criterion_moves_to_exit_gates():
    kept, gates = split_criteria(
        [
            "Root cause found with exact transcript",
            "Independent review PASS on the successor commit before merge",
        ]
    )
    assert kept == ["Root cause found with exact transcript"]
    assert len(gates) == 1
    assert gates[0]["owner"] == "seraph"
    assert "Independent review PASS" in gates[0]["criterion"]


def test_self_satisfiable_criteria_are_all_kept():
    criteria = ["Tests pass", "Ruff and formatting pass"]
    kept, gates = split_criteria(criteria)
    assert kept == criteria
    assert gates == []


def test_nothing_is_lost_in_the_split():
    """Every input criterion appears in exactly one output."""
    criteria = ["Tests pass", "Approved by the operator", "Docs updated"]
    kept, gates = split_criteria(criteria)
    recovered = kept + [g["criterion"] for g in gates]
    assert sorted(recovered) == sorted(criteria)


def test_a_card_with_only_gate_criteria_keeps_an_empty_list():
    kept, gates = split_criteria(["Independent review PASS before merge"])
    assert kept == []
    assert len(gates) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcapstone && python -m pytest tests/test_backfill_exit_gates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backfill_exit_gates'`

- [ ] **Step 3: Write the script**

Create `skcapstone/scripts/fleet/backfill_exit_gates.py`:

```python
#!/usr/bin/env python3
"""Split gate language out of acceptance_criteria on the measured tail.

Scope is deliberately narrow: cards still OPEN and claimed 3 or more times.
That is the tail that actually costs dispatch cycles. Closed cards are never
touched, and the event ledger is append-only, so the pre-split state remains
recoverable.

Dry run by default. Pass --apply to write.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

GATE_LANGUAGE_RE = re.compile(
    r"independent review|reviewer|review pass|before merge|approval|approved by"
    r"|sign-?off|merged",
    re.I,
)
DEFAULT_GATE_OWNER = "seraph"


def split_criteria(criteria: list[str]) -> tuple[list[str], list[dict]]:
    """Return (worker-owned criteria, derived exit gates). Nothing is dropped."""
    kept: list[str] = []
    gates: list[dict] = []
    for criterion in criteria:
        text = str(criterion)
        if GATE_LANGUAGE_RE.search(text):
            gates.append(
                {
                    "gate": "independent-review",
                    "owner": DEFAULT_GATE_OWNER,
                    "criterion": text,
                }
            )
        else:
            kept.append(text)
    return kept, gates


def card_actions(card_dir: Path) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    events_dir = card_dir / "events"
    if not events_dir.exists():
        return counts
    for log in events_dir.glob("*.jsonl"):
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                counts[json.loads(line).get("action")] += 1
            except Exception:
                continue
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(Path.home() / ".skcapstone"))
    parser.add_argument("--min-claims", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    home = Path(args.home)
    candidates = []
    for card_dir in (home / "cards").iterdir():
        core_path = card_dir / "core.json"
        if not core_path.exists():
            continue
        core = json.loads(core_path.read_text(encoding="utf-8"))
        criteria = core.get("acceptance_criteria") or []
        if not criteria:
            continue
        counts = card_actions(card_dir)
        if counts.get("complete") or counts.get("void"):
            continue
        if counts.get("claim", 0) < args.min_claims:
            continue
        kept, gates = split_criteria([str(c) for c in criteria])
        if gates:
            candidates.append((card_dir, core, kept, gates))

    print(f"cards in scope: {len(candidates)}")
    for card_dir, _core, kept, gates in candidates:
        print(f"  {card_dir.name}: {len(kept)} kept, {len(gates)} moved to exit_gates")

    if not args.apply:
        print("dry run. pass --apply to write")
        return 0

    for card_dir, core, kept, gates in candidates:
        core["acceptance_criteria"] = kept
        core["exit_gates"] = gates
        core["spec_version"] = 2
        (card_dir / "core.json").write_text(
            json.dumps(core, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"  split {card_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd skcapstone && python -m pytest tests/test_backfill_exit_gates.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Dry run against the real store**

Run: `ssh skuser01@chiap08 'cd ~/work/skcapstone && ~/.skenv/bin/python scripts/fleet/backfill_exit_gates.py --home ~/.skcapstone'`
Expected: a card count well under 160, since the filter is open AND claimed 3 or more times. Record the exact number; it is the baseline for the verification table in the spec.

- [ ] **Step 6: Apply**

Run: `ssh skuser01@chiap08 'cd ~/work/skcapstone && ~/.skenv/bin/python scripts/fleet/backfill_exit_gates.py --home ~/.skcapstone --apply'`

- [ ] **Step 7: Verify the split held**

Run:
```bash
ssh skuser01@chiap08 '~/.skenv/bin/python - <<PY
import json, glob
v2 = [p for p in glob.glob("/home/skuser01/.skcapstone/cards/*/core.json")
      if json.load(open(p)).get("spec_version") == 2]
print("spec_version 2 cards:", len(v2))
bad = [p for p in v2
       if any("independent review" in str(c).lower()
              for c in json.load(open(p)).get("acceptance_criteria") or [])]
print("v2 cards still carrying gate language in criteria:", len(bad))
PY'
```
Expected: the v2 count matches Step 5, and the bad count is 0.

- [ ] **Step 8: Commit**

```bash
git add scripts/fleet/backfill_exit_gates.py tests/test_backfill_exit_gates.py
git commit -m "feat(fleet): backfill exit_gates on the measured tail

Scope is open cards claimed 3 or more times, not all 160 with gate
language. Closed cards are never touched and the ledger is append-only,
so the pre-split state stays recoverable.

The split moves criteria; it never rewrites or drops one."
```

---

### Task 10: Let callers supply a real reason

Task 3 records `unspecified` when no reason is given. This task gives the two
real callers a way to say something better, which is what moves coverage off the
sentinel.

**Files:**
- Modify: `skcapstone/src/skcapstone/cli/coord.py` (the `release-claim` command)
- Modify: `skcapstone/scripts/fleet/the worker launch string` (the worker shell trap)
- Test: `skcapstone/tests/test_coord_release_reason.py` (create)

**Interfaces:**
- Consumes: `validate_abandon_reason(value) -> str` from Task 2.
- Produces: `skcapstone coord release-claim <cid> --owner <w> --abandon-reason <r>`. The flag is optional; omitting it preserves today's behaviour exactly.

- [ ] **Step 1: Write the failing test**

Create `skcapstone/tests/test_coord_release_reason.py`:

```python
"""release-claim accepts an optional reason and passes it through."""

from __future__ import annotations

from click.testing import CliRunner

from skcapstone.cli.coord import coord


def test_release_claim_accepts_an_abandon_reason():
    result = CliRunner().invoke(coord, ["release-claim", "--help"])
    assert result.exit_code == 0
    assert "--abandon-reason" in result.output


def test_abandon_reason_is_optional():
    """Omitting the flag must stay valid; the worker trap does not pass one."""
    result = CliRunner().invoke(coord, ["release-claim", "--help"])
    assert "[required]" not in result.output.split("--abandon-reason")[1][:120]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skcapstone && python -m pytest tests/test_coord_release_reason.py -v`
Expected: FAIL. `--abandon-reason` is not in the help output.

- [ ] **Step 3: Add the flag**

In `src/skcapstone/cli/coord.py`, on the `release-claim` command, add:

```python
@click.option(
    "--abandon-reason",
    default=None,
    help=(
        "Why the worker stopped: criteria-unsatisfiable, dependency-unsatisfied, "
        "capability-missing, error, superseded. Omit and it records unspecified."
    ),
)
```

Pass it through to the `append_event` call as `abandon_reason=abandon_reason`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd skcapstone && python -m pytest tests/test_coord_release_reason.py -v`
Expected: PASS, 2 tests

- [ ] **Step 5: Give the dispatcher's release calls a reason**

IMPORTANT: on `origin/main` the worker does NOT release its own claim. The
launch string comments say "one CardStore fence. The child must never release
independently" and the child trap only runs `stop_beat`. The DISPATCHER releases,
at several `coord release-claim` call sites.

Find them:

```bash
grep -n '"coord", *"release-claim"\|"coord","release-claim"' scripts/fleet/skfleet-rotate.py
```

Each call is a list passed to subprocess, shaped like:

```python
[SKC, "coord", "release-claim", cid, "--owner", owner, ...]
```

For each one, append the reason that call site actually models. Do not use a
single blanket value: the whole point is attribution. Read the surrounding code
to decide, and record your mapping in the report file. Expected shapes:

- a reaper or stale-claim sweep releasing a dead worker: `"--abandon-reason", "error"`
- a reassignment handing work to another owner: `"--abandon-reason", "superseded"`
- a dependency or eligibility refusal: `"--abandon-reason", "dependency-unsatisfied"`

If a call site's intent is genuinely unclear from its context, use
`"--abandon-reason", "unspecified"` and say so in the report. An honest
`unspecified` is better than a confident wrong label.

- [ ] **Step 6: Verify the launcher string still parses**

Run: `cd skcapstone && python -c "import ast; ast.parse(open('scripts/fleet/skfleet-rotate.py').read()); print('parses')"`
Expected: `parses`

- [ ] **Step 7: Run the fleet suite**

Run: `cd skcapstone && python -m pytest tests/ -k skfleet -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/skcapstone/cli/coord.py scripts/fleet/skfleet-rotate.py tests/test_coord_release_reason.py
git commit -m "feat(coord): optional --abandon-reason on release-claim

Task 3 records unspecified when no reason is given. This gives callers a
way to say something better.

The worker shell trap cannot know intent, but it knows the signal, so
the HUP/INT/TERM path now records error. A clean exit still records
unspecified, which is honest: a worker that exited without saying why
did not say."
```

---

## Verification after all tasks

Run from the spec's section 6 table:

```bash
# no card exceeds the ceiling plus one
ssh skuser01@chiap08 '~/.skenv/bin/python - <<PY
import json, glob, collections
worst = 0
for d in glob.glob("/home/skuser01/.skcapstone/cards/*/"):
    c = collections.Counter()
    for f in glob.glob(d + "events/*.jsonl"):
        for l in open(f, errors="ignore"):
            try: c[json.loads(l).get("action")] += 1
            except Exception: pass
    if not c.get("complete") and not c.get("await_gates"):
        worst = max(worst, c.get("claim", 0))
print("worst open-card claim count:", worst)
PY'
```
Expected: at most 6 for any card claimed after the ceiling landed. Cards that already exceeded it keep their historical count; the ledger is append-only.

Then confirm `abandon_reason` coverage is rising above 95 percent within seven days, per the spec's verification table.
