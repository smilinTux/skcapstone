"""Bind a provisional PASS to the bytes a reviewer will actually verify.

A provisional PASS (``PASS_FOR_REVIEW``, ``PASS_FOR_REREVIEW``) is not an
outcome. It is a REQUEST: it asks the fleet to open a governed review card and
put an independent reviewer on another host in front of a specific candidate.
The review opener therefore refuses to open one unless the verdict names the
candidate and its sha256, plus the typed commit, tree and ref that bind it to a
real source revision. That refusal is correct: handing a reviewer a binding that
never existed is worse than opening nothing.

MEASURED ON THE LIVE chi BOARD, 2026-09-18. ``OPENED_REVIEW`` was 0 across 14
days and 1,660 rotations. 214 cards logged ``OPEN_REVIEW_EVIDENCE_BLOCKED``
every cycle, and 242 distinct cards accrued over the 14 days at 4 to 13 a day.
Of the 354 cards ever reported blocked that way:

    305   PASS_FOR_REVIEW present ONLY as a kanban overlay ``link`` row
    346   no hash-bound candidate evidence anywhere, in either store

The overlay row was byte-for-byte what ``skcapstone coord link <card> verdict
PASS_FOR_REVIEW`` writes. That was not worker sloppiness. ``coord link`` builds
a ``CardEvent``, a pydantic model with a fixed field set holding no
``candidate_path`` and no ``candidate_sha256``, so a verdict written through it
structurally CANNOT carry the binding. And it was the only verdict command the
fleet had: the worker brief, ``AGENTS.md``, ``coord briefing`` and
``coord --help`` all named it and nothing else. Every worker did exactly as it
was told, and the instruction could not produce an admissible verdict.

So this module supplies the missing half. ``candidate_evidence`` builds the
native payload, hashing the file ITSELF rather than trusting a digest the caller
typed, and ``validate_provisional_verdict`` refuses the class of verdict that
needs a binding on the write path that cannot carry one. The BLOCKED contract
took the same route in ``blocked_verdict``: the rule was already in the worker
brief, asking was not enough, so it is refused at the write path instead.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

#: Link keys that carry a card's outcome. Matched on shape, as the store has
#: several spellings of the same idea.
_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)

#: The same token family the review opener treats as provisional
#: (``_PROVISIONAL_PASS_RE`` in ``scripts/fleet/skfleet-rotate.py``). A plain
#: ``PASS`` is a terminal outcome that opens no review, so it is untouched.
_PROVISIONAL_PASS_RE = re.compile(r"^\s*(PASS_FOR_[A-Z_]+|PASS_READY_[A-Z_]+)\b", re.IGNORECASE)

_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_REF_RE = re.compile(r"(?:refs/heads/|https://)\S+")

#: Where a candidate has to live to survive the producer's worktree. Reviews in
#: this estate are deliberately run on a different host to prove independence,
#: and a reviewer on another host cannot read a worktree that has been removed.
SHARED_EVIDENCE_ROOT = ".skcapstone/evidence/work"

REMEDY = (
    "Record a provisional PASS with the verdict verb, which binds it to the bytes "
    "a reviewer on another host will verify:\n"
    "  skcapstone coord verdict <card_id> PASS_FOR_REVIEW \\\n"
    "    --candidate ~/.skcapstone/evidence/work/<card_id>/<file> \\\n"
    "    --commit $(git rev-parse HEAD) \\\n"
    "    --tree $(git rev-parse HEAD^{tree}) \\\n"
    "    --ref refs/heads/<branch> --agent <your_name>\n"
    "Record it LAST when you can. A generation is current only while nothing\n"
    "invalidating follows it: a new outcome link, a blocked_on chain, or an\n"
    "evidence_sha256 link supersedes it and the review never opens. Any other\n"
    "link you make after the verdict, such as your own evidence or commit\n"
    "reference, is fine and does not disturb it."
)


def is_provisional_pass(key: str, value: str) -> bool:
    """Whether this link records a PASS that still owes an independent review."""
    return bool(
        _OUTCOME_KEY_RE.search(str(key or "")) and _PROVISIONAL_PASS_RE.match(str(value or ""))
    )


def validate_provisional_verdict(key: str, value: str) -> None:
    """Refuse a provisional PASS on a write path that cannot bind a candidate.

    Raises:
        ValueError: when the link would record a provisional PASS. The message
            names the verb that can record it properly.
    """
    if not is_provisional_pass(key, value):
        return
    token = _PROVISIONAL_PASS_RE.match(str(value).strip()).group(1).upper()
    raise ValueError(
        f"{token} asks for a governed review, and a review cannot open without the "
        "candidate bytes and the source revision it reviews. A coord link event has "
        "no field for either, so this would record a request nothing can act on: "
        "measured on this fleet, 214 cards did exactly that and no review opened for "
        "14 days.\n" + REMEDY
    )


def candidate_evidence(
    candidate: str | os.PathLike[str], commit: str, tree: str, ref: str
) -> dict[str, str]:
    """Return the native verdict payload binding one candidate to one revision.

    The digest is computed from the file, never taken from the caller: a hash a
    producer types is a claim, and a hash the producer path computes is a
    binding. The typed triple is validated against exactly the shapes the review
    opener accepts, so a verdict this returns cannot be admitted here and then
    silently refused there.

    Raises:
        ValueError: when the candidate is unreadable, or the source binding is
            not a shape the opener can verify.
    """
    path = Path(os.path.expanduser(str(candidate)))
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(
            f"candidate {path} cannot be read ({exc.strerror or exc}). A sha256 with no "
            "reachable bytes is not evidence, it is a promise that expired: write the "
            f"candidate under ~/{SHARED_EVIDENCE_ROOT}/<card_id>/ so a reviewer on "
            "another host can still read it after your worktree is gone."
        ) from None
    if not path.is_file():
        raise ValueError(f"candidate {path} is not a regular file")
    for name, value in (("commit", commit), ("tree", tree)):
        if not _SHA1_RE.fullmatch(str(value or "").strip().lower()):
            raise ValueError(
                f"--{name} must be a 40-character hex object id, got {value!r}. "
                f"Use git rev-parse HEAD{'^{tree}' if name == 'tree' else ''}."
            )
    if not _REF_RE.fullmatch(str(ref or "").strip()):
        raise ValueError(
            f"--ref must be a fetchable reference starting with refs/heads/ or https://, "
            f"got {ref!r}. A bare branch name does not say which repository to fetch from."
        )
    return {
        "candidate_path": str(path.resolve()),
        "candidate_sha256": hashlib.sha256(payload).hexdigest(),
        "candidate_commit": str(commit).strip().lower(),
        "candidate_tree": str(tree).strip().lower(),
        "candidate_ref": str(ref).strip(),
    }


def record_guarded_verdict(
    home: Path,
    card_id: str,
    agent: str,
    outcome: str,
    payload: dict[str, str],
    *,
    expected_source_revision: str,
    expected_claim_revision: str,
    transition_id: str,
) -> dict:
    """Record once under the native card lock only while exact inputs remain current.

    The optional CLI guard supports a controller relaying terminal worker
    evidence. It does not grant review, completion, merge or deployment.
    A replay must match the original request and still be the current outcome
    of the unchanged claim. It cannot silently adopt a later generation.
    """
    from skcoord.card_store import card_mutation_lock

    from .card_store import CardStore
    from .seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome, card_revision

    for name, value, size in (
        ("source revision", expected_source_revision, 64),
        ("claim revision", expected_claim_revision, 32),
        ("transition id", transition_id, 64),
    ):
        if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{size}}}", value):
            raise ValueError(f"guarded verdict invalid {name}")
    request = {
        "writer": agent,
        "verdict": outcome,
        "expected_source_revision": expected_source_revision,
        "expected_claim_revision": expected_claim_revision,
        **payload,
    }
    with card_mutation_lock(home, card_id):
        current_payload = candidate_evidence(
            payload["candidate_path"],
            payload["candidate_commit"],
            payload["candidate_tree"],
            payload["candidate_ref"],
        )
        if current_payload != payload:
            raise ValueError("guarded verdict candidate changed")
        store = CardStore(home)
        card = store.fold(card_id)
        if (
            card is None
            or card.owner != agent
            or card.meta.get("_claim_revision") != expected_claim_revision
            or card.meta.get("claim_conflicts")
            or card.archived
            or card.status.value not in {"ready", "doing", "review"}
        ):
            raise ValueError("guarded verdict claim changed")
        stable_revision = card_revision(card)
        matches = [
            event
            for event in store._read_events(card_id)
            if event.get("transition_id") == transition_id
        ]
        if matches:
            if len(matches) != 1:
                raise ValueError("guarded verdict ambiguous transition")
            existing = matches[0]
            if (
                existing.get("action") != "verdict"
                or any(existing.get(key) != value for key, value in request.items())
                or existing.get("guarded_card_revision") != stable_revision
                or _latest_outcome(store, card_id).get("event_id") != existing.get("event_id")
            ):
                raise ValueError("guarded verdict transition changed")
            return existing
        if LiveCardStoreGateway(home).read_card(card_id).revision != expected_source_revision:
            raise ValueError("guarded verdict source changed")
        return store.append_event(
            card_id,
            "verdict",
            agent,
            verdict=outcome,
            **payload,
            expected_source_revision=expected_source_revision,
            expected_claim_revision=expected_claim_revision,
            guarded_card_revision=stable_revision,
            transition_id=transition_id,
        )
