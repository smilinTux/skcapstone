# SKCapstone — Agent Onboarding (Universal)

This file provides instructions for ANY AI agent working on SKCapstone,
regardless of tool, IDE, or platform.

## Orchestrating the fleet? Run FROM the estate you are controlling

A controller must run inside the estate it controls. `nor` and `chi` have
disjoint Syncthing shares and CardStores, so a chi rollout run from `noroc2027`
reads every chi readiness verdict as missing, and halts on node one as if it
failed a real check. Rule and known-good entrypoints:
[docs/fleet/runbook-chiap08-controller.md](docs/fleet/runbook-chiap08-controller.md).

## Which section is for you

Your card's title tells you which role you are claimed as. Read that first,
not the seat label alone, because the label can be absent on older cards.

- Title has no `[REVIEW` tag: you are the **producer/implementer**. Steps 1
  through 6 below are for you.
- Title starts with `[REVIEW` (for example `[REVIEW][S] Review provisional
  outcome for <parent_id>`): you are the **reviewer**. Jump to
  "Reviewer (`[REVIEW]` cards)" below. Steps 1 through 6 do not apply to
  you; a review card is completed by a verdict link, not by doing the work
  Step 4 describes.
- Anything else (a card whose writer identity in the event trail is `link`
  or `mero` rather than a worker you claimed): see "Other lifecycle seats"
  below. These are not cards a worker agent claims and works through
  Steps 1-6.

Before either role, read "Reading the board without blowing your context"
near the end of this file. It is the difference between a targeted read and
one that burns your whole context window on a single `coord status`.

## Step 1: Learn the Coordination Protocol

```bash
skcapstone coord briefing
```

This single command prints everything you need:
- The complete multi-agent coordination protocol
- JSON schemas for tasks and agent files
- CLI commands for all operations
- A live snapshot of current tasks and who's working on what

For machine-readable output:

```bash
skcapstone coord briefing --format json
```

## Step 2: Check In

```bash
skcapstone coord status
```

See what tasks are open, what's claimed, and which agents are active.

## Coordination Write Boundary

All worker verdict, evidence, status, claim, label, dependency, and lifecycle
writes must use `skcapstone coord`. Never create, append, rewrite, rename, or
delete CardStore JSONL directly. Use CLI reads for normal verification. Raw file
inspection is reserved for emergency operator diagnostics.

Good:

```bash
# A PASS that owes an independent review must bind the bytes the reviewer
# verifies. coord link has no field for a candidate, so it refuses this class.
skcapstone coord verdict <card_id> PASS_FOR_REVIEW \
  --candidate ~/.skcapstone/evidence/work/<card_id>/<file> \
  --commit $(git rev-parse HEAD) --tree $(git rev-parse HEAD^{tree}) \
  --ref refs/heads/<branch> --agent <your_name>
# Record it LAST when you can, but the rule is narrower than it used to
# sound. As of PR #837, a generation is only invalidated by a link that
# comes after it AND is one of: a new outcome-shaped link (another
# verdict/outcome/result/disposition/review_decision key), a blocked_on
# chain, or an evidence_sha256 link. Any other link after the verdict,
# including your own evidence path or a commit reference, is fine and does
# not disturb it. `skcapstone coord verdict --help` carries this as the
# live contract; trust that over any older note that says "anything."

# A terminal outcome (plain PASS, BLOCKED) has no candidate to bind:
skcapstone coord link <card_id> verdict PASS --agent <your_name>
skcapstone coord link <card_id> evidence <repo-relative-path> --agent <your_name>
skcapstone coord move <card_id> review --agent <your_name>
```

Bad: opening a file below `cards/<card_id>/events/` or
`coordination/card_events/` and writing JSONL yourself. An agent that did
exactly this left 54 unreadable lines that every fold on every host paid for
until they were cleaned up. `skcapstone coord` is the only write boundary,
with no exception for "just this once."

If a coordination CLI or MCP call fails to import, validate, or write, record
the exact error and stop dependent writes. Never fall back to direct JSONL
events. For a broken CardStore chain, preserve the original bytes and hash,
identify the card, writer file, and failing event, and treat later events as
untrusted. Resume only after an exact claimed recovery task uses a reviewed
mediated command and strict fold readback. This does not add a human approval
gate.

`core.json` is **not** the card's title. It keeps the birth title write-once;
the *folded* title (what the dispatcher actually reads) comes from event
state, and `authoritative_claimability` overwrites it from the latest
`describe` event. Checking `core.json` and concluding a title is healthy is a
trap: read the folded card, not the birth record.

### `coord edit` (renamed from `coord describe`): titles are content, not CLI flags

**The CLI verb is now `coord edit`.** `coord describe` still works as a
deprecated alias and warns on stderr, but it will be removed. The rename
happened because every other CLI spells "describe" as a READ (kubectl, aws,
docker) while here it WRITES, and that collision reliably sent agents to the
wrong verb. **To read one card, use `coord show <id>`** (see "Reading the board
without blowing your context"). The MCP tool is still named `coord_describe`;
only the CLI verb moved.

`--title`/`title` take the title text itself, never a flag name and never a
placeholder. 118 real corruption events, 42 in one day, were exactly these
two mistakes:

Good (CLI):

```bash
skcapstone coord edit <card_id> --title "[M] Fix login retry backoff" --agent <your_name>
```

Good (MCP, same event, same effect):

```json
{"tool": "coord_describe", "task_id": "<card_id>", "title": "[M] Fix login retry backoff", "agent": "<your_name>"}
```

Bad, an option NAME typed as the option VALUE (the caller confused the CLI
signature with the MCP one and put `--description` *inside* the title field
instead of the actual text):

```json
{"tool": "coord_describe", "task_id": "<card_id>", "title": "--description", "agent": "<your_name>"}
```

This is refused (`refusing title '--description': it starts with '-'...`),
not written, but only because the guard exists. Put the real title text in
the field; never a flag name.

Bad, a placeholder from testing/poking the tool, also refused:

```json
{"tool": "coord_describe", "task_id": "<card_id>", "title": "x", "agent": "<your_name>"}
```

**Every title needs exactly one size marker: `[S]`, `[M]`, `[L]`, or
`[XL]`.** A title with none is silently un-routable: the dispatcher drops the
card from the candidate scan with no log line, no error, nothing. `[M]` is
the safe default when you're unsure. `[L]` fails closed on any estate that
has no L-class provider admitted, so don't reach for it out of habit.

## Step 3: Claim Work

```bash
skcapstone coord claim <task_id> --agent <your_name>
```

## Step 4: Do the Work

Follow the project conventions:
- Python 3.11+, PEP 8, type hints, black formatting
- Pydantic for data models
- Pytest tests (happy path + edge case + failure case)
- Google-style docstrings on every function
- Max 500 lines per file

**Changelog: add a NEW file, never edit `CHANGELOG.md`.** Any PR touching `src/**`
or `pyproject.toml` must record a changelog entry or the `docs / docs-check`
tier-2 gate fails it. Record it as a new fragment:

```bash
cat > changelog.d/<your-branch-slug>.md <<'ENTRY'
- Card `<id>`: what changed, and what was observably wrong before.
ENTRY
```

One file per PR means two concurrent PRs never touch the same lines, so the
rebase conflict that a shared `CHANGELOG.md` guarantees cannot happen. Editing
`CHANGELOG.md` directly still satisfies the gate, but with other PRs open you
will be resolving a conflict. See `changelog.d/README.md`.

## Step 5: Complete

A source card (code that lands in a repo) is complete when exactly three
things are true: an independent reviewer recorded PASS on the exact commit,
the tests are green on that same commit, and the reviewed code is merged on
the target branch (default `origin/main`). An operator (`lumina-nor` or
`jarvis`) closes it with `coord accept`, which checks those facts and closes
the source card and its review together:

```bash
skcapstone coord accept <source_card> --agent lumina-nor --repo-path <local clone>
```

Builds run in lanes; reviews run in reviewer panes. There is no dispatcher
that opens, retires or regenerates reviews for you, and no test-plan
generation, profile qualification, applicability receipt or finish journal to
satisfy. See `src/skcapstone/simple_accept.py` for the full fact list.

A card with no review (a chore, a doc card) is still closed directly:

```bash
skcapstone coord complete <task_id> --agent <your_name>
```

## Step 6: Create Discovered Work

```bash
skcapstone coord create --title "What needs doing" --by <your_name>
```

## Directory Reference

| Path | Purpose |
|------|---------|
| `src/skcapstone/` | Core framework modules |
| `tests/` | Pytest test suite |
| `docs/` | Architecture and design docs |
| `~/.skcapstone/coordination/` | Syncthing-synced task board |
| `~/.skcapstone/coordination/tasks/` | Task JSON files (immutable) |
| `~/.skcapstone/coordination/agents/` | Agent status JSON files |

## How Sync Works

The `~/.skcapstone/` directory is synchronized via Syncthing across all
devices in the mesh. No SSH, no APIs, no cloud services — just encrypted
peer-to-peer file sync. Create a task here, it appears everywhere.

## Reviewer (`[REVIEW]` cards)

Reviews run in reviewer panes, not through an automated dispatcher. If a
card's title starts with `[REVIEW`, a reviewer opened it against a specific
producer's outcome and you are the independent reviewer. Everything below is
derived from reading real review cards that actually closed, not from
guessing at the shape the code should have.

### How to tell you are the reviewer

Your card title starts with `[REVIEW` (for example `[REVIEW][S] Review
provisional outcome for 1960b107`). Your identity must differ from the
producer's.

### Find your parent and candidate

Your card's **description**, not its title, is the source of truth. It is
written in one fixed template by the card opener (`coord review-work`). A real example, verbatim
from card `b34ca6f9`:

```
Independently review parent 1960b107 at outcome 2026-09-19T23:52:57.664797+00:00
(PASS_FOR_REVIEW). Producer identity: pi-glm-chiap03-1960b107. Candidate
evidence: /home/skuser01/.skcapstone/evidence/work/1960b107/card-1960b107-
qualification-receipt.md sha256=65a2fcc7c9ab656501c081d6661da95ea43d223209e
ee5cc9239724f66643f22. Outcome generation: c68db1d290b40f8b70c6c048ac6e59d3
19746788ecdedaefdbbbdd44ca02200b. Reviewer identity must differ. Candidate
commit: a38c1d67a6892ce9cba94ec2386d846047663cdd. Candidate tree:
02e44d8d3858401b8ee27191584e622200f2e238. Candidate ref:
refs/heads/feat/1960b107-maintenance-governance-qualification.
```

Pull out of that text: the parent card id (`1960b107`), the producer
identity you must differ from, the candidate evidence path and its
sha256, the commit/tree/ref you are reviewing, and the outcome generation
this review is bound to. The same facts are duplicated into `meta` on the
card (`producer_identity`, `candidate_evidence_sha256`,
`link_source_card`, `link_head_revision`, and often `repository`), but
there is no CLI command that reads `meta` back to you; the description is
what you can actually retrieve without operator help (see "does not work"
list below for why `coord describe <id>` alone will not do this).

### Verify the candidate before you judge it

```bash
sha256sum /home/skuser01/.skcapstone/evidence/work/1960b107/card-1960b107-qualification-receipt.md
# must equal the sha256= value from the description above
```

If the description also names a commit/tree/ref, verify those against the
actual repository state (`git cat-file -e <commit>`, `git rev-parse
<ref>`) before writing a PASS. A PASS that never checked the bytes it names
is exactly the silence PR #241 was written to stop (see below).

### The verdict sequence that actually closes a review

This is not inferred from the code, it is read off real review cards that
closed cleanly under the CURRENT completion gate
(`src/skcapstone/review_verdict.py`, tightened by PR #567 and #690,
confirmed against cards closed after 2026-09-13). Two examples, one PASS
on the skcapstone repo and one FAIL, both closed on the first attempt:

```bash
# Evidence links first. Use key names that do NOT contain "verdict",
# "outcome", "result", "disposition", or "review_decision" for anything
# that is not your final canonical line (see the trap below).
skcapstone coord link <review_id> pr <pr_url> --agent <your_name>
skcapstone coord link <review_id> head <candidate_commit_sha> --agent <your_name>
skcapstone coord link <review_id> base <base_sha> --agent <your_name>
skcapstone coord link <review_id> candidate_evidence_sha256 <sha256> --agent <your_name>
skcapstone coord link <review_id> evidence <path_or_test_files> --agent <your_name>
skcapstone coord link <review_id> finding "PASS exact head ... / base ..." --agent <your_name>

# The verdict itself: byte-exact "PASS" or "FAIL", nothing else in this value.
skcapstone coord link <review_id> verdict PASS --agent <your_name>

# PASS on a skcapstone-repo card additionally needs all six of these,
# each byte-exact "SUCCESS" (src/skcapstone/review_verdict.py:_REQUIRED_CI_LINK_KEYS):
skcapstone coord link <review_id> ci_check_docs SUCCESS --agent <your_name>
skcapstone coord link <review_id> ci_check_gitleaks SUCCESS --agent <your_name>
skcapstone coord link <review_id> ci_check_lint SUCCESS --agent <your_name>
skcapstone coord link <review_id> ci_check_shim_imports SUCCESS --agent <your_name>
skcapstone coord link <review_id> ci_check_python311 SUCCESS --agent <your_name>
skcapstone coord link <review_id> ci_check_python312 SUCCESS --agent <your_name>

skcapstone coord complete <review_id> --agent <your_name>
```

For a PASS on a card whose `meta.repository` is a DIFFERENT repo
(skdashboard, skgateway, sklegal, skharness, skcoord, and so on), skip the
six `ci_check_*` links and record one `hosted_checks` link instead, with
the value matching this exact shape (verified on real closed cards
`c6298ac5` and `aa0202b8`):

```bash
skcapstone coord link <review_id> hosted_checks "4/4 SUCCESS at exact head 4d66082fa08ea71cfec106c5e2badcff9ad90449" --agent <your_name>
# <passed>/<total> SUCCESS at exact head <the 40-hex commit your meta.link_head_revision names>
skcapstone coord complete <review_id> --agent <your_name>
```

FAIL needs no CI checks at all; only the exact word `FAIL` and then
complete (verified on real closed card `c30a0709`, 8 events total, verdict
recorded first, evidence links after it, complete last).

A BLOCKED verdict is also accepted, but only in this shape
(`src/skcapstone/review_verdict.py:_is_terminal_verdict`):

```bash
skcapstone coord link <review_id> verdict "BLOCKED blocked_on=capability referent=ac:1" --agent <your_name>
skcapstone coord complete <review_id> --agent <your_name>
```

Both `blocked_on=` and `referent=` must be present and non-empty; the
category is one of `dependency`, `card`, `human`, `capability`
(`src/skcapstone/review_admission.py:parse_blocked_on_link`).

### Mistakes to avoid, each one measured against real cards

1. **Never record a verdict any way other than `coord link <id> verdict
   <value>`.** Card `b34ca6f9` itself carries 16 raw `action=verdict`
   events with an empty key and empty value, spread across 17 claim/18
   release cycles, and zero completions. Those events come from a
   different, older write path (a legacy "overlay" verdict action) that
   `skcoord`'s CardStore fold never maps (`_OVERLAY_TO_STORE_ACTION` in
   `card_store.py` maps `move`, `set_priority`, `set_swimlane`,
   `add_label`, `remove_label`, `link`, `assign`, `unassign`, `describe`;
   there is no `verdict` entry), so they are silently invisible to
   `coord complete`'s check and every one of those 16 writes accomplished
   nothing. Review card `61f972ed` hit the identical trap on
   2026-08-28 and sat unfixed until 2026-09-19, when it had to be
   hand-repaired with a fresh `coord link ... verdict PASS`.
2. **Never write a second link whose KEY merely contains the substring
   "verdict" after your real one.** `recorded_verdict()` matches any link
   key containing `verdict|outcome|result|disposition|review_decision`
   and takes whichever has the LATEST timestamp, not the one literally
   named `verdict`. Keys like `verdict_sha256`, `verdict_artifact`, or an
   explanatory `verdict_recovered` written after your canonical line will
   silently become the one `coord complete` reads, and it will not be the
   exact `PASS`/`FAIL` string, so completion fails with "nonterminal
   verdict". The `61f972ed` repair walked straight into this on its own
   recovery note and had to re-record `verdict` a second time with a
   fresher timestamp to win the race back.
3. **`skcapstone fleet` is a different subsystem from `skcapstone coord`.**
   `fleet` is the SKWorld node/service control plane (`fleet get
   {cronjobs,modelservers,agents,configs,profiles}`); `workers` is not a
   valid resource there (`Error: unknown resource: 'workers'`), and even
   the valid resources have nothing to do with review work. Stay in
   `coord` for everything in this section.
4. **Do not self-review.** If your claiming agent identity equals (or
   seat-normalizes to) the `Producer identity:` named in the card
   description, `coord claim` is refused outright:
   `governed review claim denied: producer-self-review, ...`
   (`src/skcapstone/review_admission.py:reviewer_candidate_reasons`). Use
   an agent name that is not the producer's.
5. **`coord describe <id>` with no flags does not read a card.** It is a
   WRITE, renamed to `coord edit` on 2026-09-21 with `describe` kept as a
   warning alias. It errors with `Pass --title and/or --description.`
   **The single-card read is now `coord show <id>`** (3,854 bytes, versus
   11,925,646 for `kanban --json`). Older notes saying "there is no CLI
   single-card read" are stale.
7. **A terminal PASS is not finished when the verdict is recorded.**
   `coord complete` refuses a PASS review card whose CI evidence is missing,
   and the refusal is the last step, so the work looks done right up until it
   is rejected. Measured 2026-09-21: of 137 review cards carrying a verdict
   but still open, **60 were refused on exactly this**, 30 for a missing
   `hosted_checks` (skcoord, sklegal), 29 for absent `ci_check_*` links, 1
   malformed. Link the evidence BEFORE you call complete, using the recipe in
   "The verdict sequence that actually closes a review" above: six
   `ci_check_*` links for a skcapstone-repo card, one `hosted_checks` pinned
   to the exact head for any other repo. **Record only what the checks
   actually report.** A `SUCCESS` link for a check that did not pass is a
   false evidence record, and that is worse than leaving the card open.
8. **A nonterminal verdict blocks completion too.** `PASS_FOR_REVIEW`,
   `FAIL_CLOSED` and `QUARANTINED` are not terminal, and `coord complete`
   rejects them with `has nonterminal verdict '<value>'`. Twelve cards sat in
   that state in the same 2026-09-21 measurement. If the review is genuinely
   finished, record a terminal `PASS`, `FAIL` or `BLOCKED` last.

## Other lifecycle seats

`LIFECYCLE_SEATS` in `src/skcapstone/lifecycle_seats.py` still names `link`,
`mero`, `seraph`, `niobe` and `atlas` as writer identities, but the
`niobe`, `atlas` and `seraph` runtime (dispatcher, seat-cycle, review
publisher, live fanout) was removed in the SKFLEET cut and its units are
gone. Only `link` and `mero` remain as operated seats, and neither is
something a worker agent claims.

- **link** (`role: integrator`): owns `pr_triage`, `reviewer_assignment`,
  `merge_eligibility`, `eligible_merge`; denied `fleet_dispatch`,
  `deployment`, `release`, `application_actuation`.
- **mero** (`role: overseer`): owns `read_only_observation`,
  `drift_measurement`, `typed_recommendation`; denied `card_claim`,
  `fleet_dispatch`, `merge`, `deployment`, `application_actuation`. Purely
  observational.

Old event trails still show `niobe`, `atlas` and `seraph` as writer
identities. Treat them as history.

## ITIL changes: an agent can never approve one

If you are asked to move an ITIL change to `approved`, you cannot, and the CLI
used to hide that. Measured 2026-09-20 on `chg-ca4d0ea5`: five successive
`itil change update --status` calls each printed a green
`Updated: chg-ca4d0ea5 -> reviewing` while the status never moved. That is
fixed now (the command reports the real refusal), but understand why it
refuses:

`_cab_resolved_status` in `skcoord/itil.py` grants approval only when

```python
approvals = [v for v in votes if v.decision == APPROVED and v.agent != prepared_by]
if any(_is_human_approval(v) for v in approvals): return "approved"
```

Two independent blocks, both deliberate:

1. **Self-approval guard.** A vote from the change's `prepared_by` is excluded.
2. **`_is_human_approval`** requires `agent == "human"` or `subject_role` in
   `{owner, approver}`. An agent vote never qualifies, whatever `--agent` says.

**`itil cab vote` records the caller's capauth-resolved identity, not
`--agent`.** On chiap08 that resolves to `jarvis`, so voting as another agent
still writes a `jarvis` vote, and if jarvis is also `prepared_by`, block 1
discards it silently. Check `prepared_by` before voting.

`reviewing -> approved` IS a legal transition in `_CHANGE_TRANSITIONS`; the CAB
guard is what refuses it, so "not a legal transition" is the wrong diagnosis.
Three distinct refusals exist: an illegal transition, the CAB guard, and an
empty `--note` on `deployed->verified` or `failed->closed`.

The only path is a human grant, which a human runs:

```bash
skcapstone itil cab authorize <change_id> --decision approved --role approver \
  --target <change_id> --scope <scope> --output ~/grant.json
skcapstone itil cab vote <change_id> --decision approved --authorization ~/grant.json
```

Do not try to route around this. Escalate to the operator and say what you need.

## Reading the board without blowing your context

Measured today against the live board: bare `skcapstone coord status`
produced 729,823 bytes across 8,861 lines (a separate measurement on
2026-09-20 recorded 730,889 bytes / 8,864 lines; the board grows, the order
of magnitude is the point). That is the entire board, every card, and it
will exhaust a small model's context on its own.

A targeted read of one card's family is small. Both of these are
equivalent and both are verified (2,295 bytes for one real open review's
parent-tag slice, measured today):

```bash
skcapstone coord status --tag parent-<parent_id>
skcapstone coord status --parent <parent_id>
```

If you only have your own review card's id and need its parent, the parent
id is in your card's description (see "Find your parent and candidate"
above); you do not need a board-wide read to get it.

**`skcapstone coord show <card_id>` is the single-card read.** Measured
2026-09-21 on the live board: **3,854 bytes** rendered, **2,851 bytes** with
`--json`, against **11,925,646 bytes** for `coord kanban --json`. That is the
same answer for roughly one three-thousandth of the context. It prints status,
column, labels, links, assignee and description, folded through
`CardStore.fold`, and falls back to the same legacy projection `kanban` uses so
the two can never disagree about a card.

```bash
skcapstone coord show <card_id>          # human-readable panel
skcapstone coord show <card_id> --json   # same card, machine-readable
```

Reach for it before any board-wide read. Checking one card's status, its
verdict link, or whether your write actually landed is a `coord show`, never a
`coord status` and never a `kanban --json`.

`skcapstone coord gates <card_id>` is also small and directly answers "why
can I not claim or dispatch this": it returns your capacity
slot, and the exact refusal reasons as JSON, for example
`{"eligible": false, "reasons": ["ownership"]}`.

**Commands that do NOT do what they look like they do, verified today:**

- `skcapstone coord kanban <card_id>`: takes no positional argument at
  all. `Error: Got unexpected extra argument (<card_id>)`.
- `skcapstone coord kanban --json`: runs, but dumps the entire board,
  11,689,722 bytes measured today. Worse than bare `coord status`, not
  better.
- `skcapstone coord describe <card_id>` with no `--title`/`--description`:
  does not read the card. `Error: Pass --title and/or --description.` It is a
  WRITE, now renamed to `coord edit`. **Use `coord show <card_id>` to read
  one card** (added 2026-09-21); before it existed the only way to read a
  single card was to render the whole board and filter it.
- `skcapstone fleet get workers`: `fleet` is a different subsystem
  entirely (SKWorld node/service control plane, not the task board).
  `workers` is not even a valid resource there: `Error: unknown resource:
  'workers' (known: cronjobs, modelservers, agents, configs, profiles)`.
- `skcapstone coord --help` alone, or `skcapstone coord status | head -60`:
  both return, but neither is a targeted read; the second one still
  pays the full generation cost of `coord status` before you throw most of
  it away. Use `--tag`/`--parent` instead.
