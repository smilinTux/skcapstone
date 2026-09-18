# Seat Lease Implementation Plan (Amendment B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Give the estate a cross-host exclusion primitive, so which host runs the
seats can stop being a hand-edited constant.

**Architecture:** A lease file on ONE designated host, guarded by that host's
`flock`, read and written over SSH. **Default OFF**: `active_host` stays
authoritative until the lease is explicitly enabled, so nothing changes on the
fleet when this lands.

**Spec:** `docs/superpowers/specs/2026-09-17-seat-exclusion-amendment.md`.

## Corrections to the amendment, measured 2026-09-18

The amendment recommended a lease backed by FalkorDB or an endpoint behind
skgateway. Both were proposed without checking. Measured:

```
FalkorDB 192.168.0.59:16379   UNREACHABLE from chiap01 and chiap08
skgateway chiap01:18790       reachable from chiap01, chiap02, chiap08
ssh host-to-host              works; chiap02 and chiap08 both reach chiap01
flock on chiap01              present, util-linux 2.39.3
```

So the FalkorDB option does not exist on this network. The gateway is reachable
but is an LLM router with no state endpoint, and adding one would be new
infrastructure.

**What actually works is simpler.** `flock` on a single host is a real mutex for
every party that reaches that host, because it is one kernel arbitrating. The
reason the CardStore claim fence fails across hosts is precisely that each host
flocks its own local copy of a Syncthing-replicated file. A lease file that lives
on exactly one host does not have that problem.

## The honest limitation, stated up front

A lease hosted on one machine does not remove a single point of failure, it
relocates it. If the lease host is down, no host can acquire and the seat layer
stops.

That is still an improvement, for one specific reason: **put the lease somewhere
other than the host that runs the seats.** Seats currently run on chiap08. A lease
served by chiap01 means chiap08 dying lets the lease expire and another host take
over, which is real failover for the seat layer. The cost is a dependency on
chiap01, which is already a universal dependency because it runs the only
skgateway and all routing goes through it.

Anyone reading this should understand they are choosing which host is critical,
not eliminating criticality.

## Global Constraints

- Python 3.12.
- CI runs `black --check src/ tests/` and `ruff check src/`. Run pytest before
  committing too, not only lint.
- No em dashes or en dashes anywhere, including comments and commit messages.
- No `Co-Authored-By` trailer you cannot evidence.
- Never write to `~/.skcapstone` from a test. Use a temp directory.
- **DEFAULT OFF is not negotiable.** Until a human enables it, `active_host`
  remains the authority and behaviour on the fleet is unchanged. Two hosts
  believing they hold the seats is the exact failure this estate has no other
  defence against.
- Reading the live fleet is fine. **No task here enables the lease anywhere.**
- Nothing may land uncalled. Six instances measured in this codebase.

---

### Task 1: The lease primitive

**Files:**
- Create: `src/skcapstone/fleet/seat_lease.py`
- Test: `tests/fleet/test_seat_lease.py`

**Interfaces:**
- `acquire(host, holder, ttl_seconds) -> LeaseResult`
- `renew(host, holder, ttl_seconds) -> LeaseResult`
- `release(host, holder) -> None`
- `current_holder(host) -> str | None`

Every operation runs under `flock` on the lease host so read-modify-write is
atomic. Use the injectable `Runner` pattern the fleet modules already use, so
tests never shell out.

**Rules:**
- An expired lease is acquirable by anyone. That is what buys failover.
- A live lease held by someone else is NOT acquirable, and saying so is a normal
  result rather than an error.
- `renew` by a non-holder must fail. Otherwise a stale host can steal a live lease.
- Every result must say WHO holds it and how long remains, because an operator
  debugging a stuck seat layer needs both.

- [ ] **Step 1: Write failing tests** covering all four operations plus: expiry
  makes a lease acquirable; a live lease blocks a second acquirer; renew by a
  non-holder fails; clock skew does not let two holders coexist.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests. Commit.**

---

### Task 2: Wire it behind a default-off switch

**Files:**
- Modify: `src/skcapstone/seat_cycle_entrypoint.py`
- Test: `tests/test_seat_cycle_entrypoint.py`

`load_control_plane` currently refuses when the local machine is not
`active_host`. That refusal is correct and stays. Add an alternative path, taken
ONLY when the lease is explicitly enabled, that asks the lease instead.

**Do not delete `active_host`.** The amendment originally proposed deleting it;
that proposal rested on the claim fence working across hosts, which it does not.
Leaving it in place means enabling the lease is a one-line revert if it misbehaves.

- [ ] **Step 1: Write failing tests:** with the lease disabled, behaviour is
  byte-identical to today, including every existing refusal; with it enabled and
  held by this host, the cycle proceeds; enabled and held by another host, it
  refuses naming the holder; enabled and unreachable, it FAILS CLOSED.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Mutation-check the fail-closed path.** Print the mutated line back
  FROM DISK before running.
- [ ] **Step 5: Commit.**

---

### Task 3: Surface and document

- [ ] **Step 1:** A command to show the current holder and remaining TTL, so an
  operator can answer "who holds the seats" without SSH.
- [ ] **Step 2:** Document how to enable it, how to roll back to `active_host`,
  and the relocated-SPOF limitation in plain terms.
- [ ] **Step 3:** CHANGELOG. State that this ships DISABLED and changes nothing
  until enabled.
- [ ] **Step 4: Commit.**
