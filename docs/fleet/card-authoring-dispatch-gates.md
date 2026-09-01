# Writing a card the fleet can actually dispatch

Every gate below is enforced by real code. The authority is
`scripts/fleet/skfleet-rotate.py` for dispatch and
`skcoord/card_store.py` for creation. If this document and the code disagree,
the code is right and this document is stale: fix it.

The reason this exists: on 2026-09-01 a card carrying a signed human
authorization with a **fixed 30 minute issue window** could not be dispatched,
because its author put `[HUMAN]` in the title. Title and `initial_labels` are
immutable. Once the approval arrived there was no way to release the card to
the fleet, and the only remaining option was manual execution against the
clock. The gate was correct. The card was authored wrong.

## The one rule that matters most

**A condition that will later be satisfied must be a removable LABEL, never a
title marker.**

Titles are immutable. `initial_labels` in `core.json` is immutable. Labels are
folded at read time from `initial_labels` plus `add_label` / `remove_label`
events, so a label can be added and removed forever. A title marker is a
life sentence.

```
BAD   [SKGW-AUTHZ-06A6-LC][S][HUMAN] Authorize a replacement lifecycle
      -> _human_gate() returns True from the title, permanently.
      -> No label change can ever release it. Manual execution only.

GOOD  [SKGW-AUTHZ-06A6-LC][S] Authorize a replacement lifecycle
      labels: human-gate, do-not-claim
      -> Same gate, same protection, but `coord label <id> human-gate --remove`
         releases it the moment the human decides.
```

Use `[HUMAN]` in a title only when the card must NEVER be machine-dispatched
under any circumstance, for example a standing decision record.

## Every dispatch gate, and how to clear it

### 1. Governed class requires exactly one parent

`_GOVERNED_CARD_CLASS = re.compile(r"\[(REVIEW|REREVIEW|REPAIR)\]")`

A title containing `[REVIEW]`, `[REREVIEW]` or `[REPAIR]` needs exactly one
`parent-<cardid>` label at creation or `coord create` refuses it:

```
ValueError: Governed card <id> requires exactly one parent-<card_id> label
```

Creation is also refused when a **live** repair already exists under that
parent:

```
ValueError: Refusing live repair duplicate for parent <id>; existing card <id> is non-terminal
```

That is a duplicate-work guard, not a bug. Either finish the sibling or pick
the correct parent. Do not retitle the card to dodge the class check.

### 2. Not-claimable labels

`_NOT_CLAIMABLE = {"not-claimable", "sprint-container", "do-not-claim"}`

Any of these keeps the card out of the pool. All three are removable:

```
skcapstone coord label <id> do-not-claim --remove --agent <you>
```

### 3. Human gate

```python
return "human-gate" in labels or "[HUMAN]" in str(core.get("title") or "").upper()
```

Two sources, only one of them removable. See the rule at the top.

A gated card is released by a human-resolution event, which
`_human_resolution_epoch()` finds by scanning for a `void` written by
`chef`, `human` or a `human-decision-recorder` writer, or a `link` /
`add_label` whose text matches the blocked-on referent. Record the resolution
against the referent you blocked on, or nothing will match.

### 4. Non-implementation labels

```
planning-only-container, do-not-claim-as-implementation, human-gate,
human-decision-recorded-no-action, no-action-authorized
```

These say "this card is not a unit of work". Containers and decision records
belong here. A card carrying one will never be implemented by a worker.

### 5. Sensitive category needs an explicit opt-in

```python
_SENSITIVE_CATEGORY = re.compile(
    r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
    r"deploy|production|release|migrat)", re.I)
_CATEGORY_OPT_IN = "dispatch-approved"
```

The regex matches the **title and labels**. If your card mentions credentials,
custody, secrets, keys, rollback, deploy, production, release or migration, it
is gated until someone adds `dispatch-approved`.

This one surprises people, because the match is textual. A card titled
"Fix the release notes typo" is sensitive-category by the word `release`. That
is deliberate: the opt-in is cheap, and a false positive costs one label.

### 6. BLOCKED backoff

A card whose latest recorded outcome is BLOCKED stays out of the pool until one
of its dependencies reaches complete. Outcomes are read through a controlled
vocabulary across `verdict`, `result`, `disposition` and `review_decision`,
because verdict has 41 spellings in this store. Re-blocking the same card
without changing anything just burns inference.

## Where labels actually live

Labels fold from two places:

```
core.json initial_labels        immutable, set at creation
card_events/*.jsonl             add_label / remove_label events, appended forever
```

`folded_labels()` reads `initial_labels`, then applies the events. It does
**not** read the per-card `cards/<id>/events/` directory, which holds the
structural lifecycle (claim, move, complete, archive, void).

This trips people up. After removing a label you will see:

```
cards/983336c1/          core.json only, no events/ directory
```

and conclude the write was lost. It was not. Check the evidence store:

```
grep -h '<cardid>' ~/.skcapstone/coordination/card_events/*.jsonl \
  | grep -E 'add_label|remove_label'
```

The legacy `coordination/tasks/<id>-*.json` file also still shows the original
labels. That store is not what the selector folds. Neither store alone answers
a question about a card; this is the two-store split, and it is why every
readback in this repo is done through a different path than the write.

## A worked example

A card that needs one human approval before the fleet may run it, touches
credentials, and repairs something:

```bash
skcapstone coord create \
  --title "[SKGW-AUTHZ-06A6-LC][S][REPAIR] Authorize a replacement service-token lifecycle" \
  --by mero --priority high \
  --tag parent-9acf44e2 \        # required: [REPAIR] is a governed class
  --tag human-gate \             # removable gate, NOT [HUMAN] in the title
  --tag do-not-claim \           # belt and braces until the human decides
  --tag skgateway --tag authz \
  --desc "..." \
  --criteria "Before claim, record an exact verbatim human authorization naming this card." \
  --criteria "..."
```

When the human decides:

```bash
# 1. record the decision as evidence, with a verifiable artifact
skcapstone coord link <id> human_authorization /path/to/AUTHORIZATION.txt
skcapstone coord link <id> human_authorization_sha256 <sha256>

# 2. release the gates
skcapstone coord label <id> human-gate --remove --agent <you>
skcapstone coord label <id> do-not-claim --remove --agent <you>

# 3. sensitive category opt-in, because the title says "credential"
skcapstone coord label <id> dispatch-approved --agent <you>

# 4. VERIFY, through a different path than the write
python3 - <<'PY'
import json, glob, os
cid = "<id>"
core = json.load(open(os.path.expanduser(f"~/.skcapstone/cards/{cid}/core.json")))
labels = list(core.get("initial_labels") or [])
for f in glob.glob(os.path.expanduser("~/.skcapstone/coordination/card_events/*.jsonl")):
    for line in open(f, errors="replace"):
        if cid not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("card_id") != cid:
            continue
        lab = e.get("label") or e.get("link_value")
        if e.get("action") == "add_label" and lab and lab not in labels:
            labels.append(lab)
        elif e.get("action") == "remove_label" and lab:
            labels = [x for x in labels if x != lab]
norm = {str(x).strip().lower().replace("_", "-") for x in labels}
print("folded:", sorted(norm))
print("blocked by not-claimable:", sorted(norm & {"not-claimable", "sprint-container", "do-not-claim"}))
print("human gate:", "human-gate" in norm or "[HUMAN]" in str(core.get("title") or "").upper())
PY
```

If step 4 still reports a gate, the card will not dispatch no matter how many
times the CLI printed success.

## Checklist before you create a card

1. Will any condition on this card be satisfied later? Put it in a **label**.
2. Does the title contain `[REVIEW]`, `[REREVIEW]` or `[REPAIR]`? Add exactly
   one `parent-<cardid>`.
3. Does the title or any label match the sensitive-category regex? Plan for
   `dispatch-approved`.
4. Is this a container or a decision record rather than work? Use a
   non-implementation label so no worker wastes a dispatch on it.
5. Does an acceptance criterion pin a **deadline**? Then check today that the
   card is dispatchable, not on the day the deadline arrives.

Point 5 is the one that cost us. The card was authored on 2026-09-01 at
02:1xZ with an issue window of 03:00:00Z, and nobody checked it was
dispatchable until 30 minutes were left.
