"""Explicit operator recovery of an interrupted step-08 source PASS.

This does not relax guarded link replay or issue a producer/reviewer verdict.
The original journal stays immutable; a separate journal continues at step 09.
"""

import re
from contextlib import ExitStack
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from ..historical_pr import history_revision
from ..review_verdict import _card_events
from ..seraph_review_cardstore import _candidate, _latest_outcome, card_revision
from ..seraph_review_contracts import _digest
from .builder_dispatch import _request_exclusion
from .production_review_finish import artifacts, native_state, once, read_json, steps
from .source_bundle import MAX_EVIDENCE, _read, _sha

SCHEMA = "skfleet.acceptance-reseal/v1"
ACTOR = "lumina-nor"
EVENT = "acceptance_reseal"
GUARDS = (
    "context_sha256",
    "finish_sha256",
    "step_sha256",
    "event_sha256",
    "source_revision",
    "review_revision",
    "source_history",
    "review_history",
    "claim_revision",
)


def acceptance_lock(directory):
    """Serialize finish and reseal before taking either native card lock."""
    return _request_exclusion(Path(directory) / "acceptance")


def _rows(store, card):
    """Keep native identities and unprojected legacy identities without double counting."""
    rows = {row.get("event_id") or _digest(row): row for row in _card_events(card, store.home)}
    for row in store._legacy_events(card):
        if row.get("origin") != "legacy-overlay":
            rows.setdefault(row.get("event_id") or _digest(row), row)
    for row in store._read_events(card):
        projected = rows.get(row["event_id"])
        if projected and any(
            projected.get(key) != row.get(key)
            for key in ("action", "writer", "ts", "seq", "link_key", "link_value")
        ):
            raise ValueError("reseal event projection conflict")
        rows[row["event_id"]] = row
    return sorted(rows.values(), key=lambda row: (row["ts"], row["writer"], row.get("seq", 0)))


def _file_hash(path):
    """Hash the exact private retained bytes, not a caller's parsed representation."""
    return _sha(_read(Path(path), MAX_EVIDENCE))


def _validate_artifacts(home, context):
    """Reuse source, committed review and qualified test validators."""
    from .production_review_evidence import inspect_proposal
    from .production_test_plan import source_state
    from .production_tests import validate_test_receipt

    artifacts(context)
    source_state(Path(context["source_workspace"]), context["test_binding"])
    review = context["review"]
    # The collected review workspace is the retained imported-review sibling.
    directory = Path(review["decision_path"]).parent
    workspace = directory / "imported-review"
    if not workspace.exists():
        # inspect_proposal fixtures and older retained proposals live in Git directly.
        workspace = directory.parents[3]
    proposal = review["proposal"]
    inspected = inspect_proposal(
        workspace,
        **{
            key: proposal[key]
            for key in ("card", "parent_card", "source_head", "source_tree", "reviewer_identity")
        },
    )
    if (
        inspected["proposal"] != proposal
        or inspected["decision_sha256"] != review["decision_sha256"]
        or inspected["report_sha256"] != review["evidence_sha256"]
        or any(
            key in review and inspected[key] != review[key]
            for key in ("review_head", "review_tree")
        )
    ):
        raise ValueError("reseal committed review changed")
    return validate_test_receipt(home, context["test_binding"], Path(context["source_workspace"]))


def _journal(directory, context, acceptance):
    """Validate the exact acknowledged prefix and unacknowledged PASS boundary."""
    binding = read_json(directory / "finish-intent.json")
    expected_binding = dict(
        controller=context["controller"], context_sha256=_digest(context), test_receipt=acceptance
    )
    if "source_claim_current" in binding:
        expected_binding["source_claim_current"] = True
    if binding != expected_binding:
        raise ValueError("reseal finish binding changed")
    expected = {
        role: context[role].get("live_revision", context[role]["revision"])
        for role in ("source", "review")
    }
    prefix = {}
    for index, (role, action, key, value) in enumerate(steps(context, acceptance)[:9]):
        path = directory / ("step-%02d.intent.json" % index)
        step = read_json(path)
        request = dict(
            binding=_digest(binding),
            index=index,
            role=role,
            action=action,
            key=key,
            value=value,
            before=expected,
            card=context[role]["card"],
            owner=context[role]["owner"],
            claim=context[role]["claim"],
        )
        if any(step.get(k) != v for k, v in request.items()):
            raise ValueError("reseal journal changed")
        prefix[path.name] = _file_hash(path)
        ack_path = directory / ("step-%02d.ack.json" % index)
        if index == 8:
            if (
                ack_path.exists()
                or any(directory.glob("step-09*"))
                or any(directory.glob("step-1*"))
                or (directory / "finished.json").exists()
            ):
                raise ValueError("reseal is only supported at unacknowledged step 08")
            return step, prefix
        ack = read_json(ack_path)
        other = "review" if role == "source" else "source"
        if (
            set(ack) != {"step_sha256", "after"}
            or ack["step_sha256"] != _digest(step)
            or set(ack["after"]) != set(expected)
            or ack["after"][other] != expected[other]
        ):
            raise ValueError("reseal acknowledgement changed")
        prefix[ack_path.name] = _file_hash(ack_path)
        expected = ack["after"]
    raise ValueError("reseal PASS boundary missing")


def _corrections(store, source, event, changes, actor, excluded):
    """Prove only the enumerated operator PR/commit corrections changed the fold."""
    current = store.fold(source["card"])
    prior = current.model_copy(deep=True)
    rows = [
        r
        for r in _rows(store, source["card"])
        if r["ts"] >= event["ts"]
        and r.get("event_id") != event["event_id"]
        and r.get("event_id") not in excluded
    ]
    if not changes or len(changes) > 2 or len(rows) != len(changes):
        raise ValueError("reseal correction history differs")
    seen = set()
    for change, row in zip(changes, rows):
        if set(change) != {"event_sha256", "key", "before", "after"}:
            raise ValueError("reseal correction fields invalid")
        key = change["key"]
        if (
            key not in {"pr", "commit"}
            or key in seen
            or row["action"] != "link"
            or row["writer"] != actor
            or _digest(row) != change["event_sha256"]
            or row.get("link_key") != key
            or row.get("link_value") != change["after"]
            or current.links.get(key) != change["after"]
            or key in current.meta
        ):
            raise ValueError("reseal correction is not exact operator metadata")
        seen.add(key)
        if not isinstance(change["before"], str) or change["before"] == change["after"]:
            raise ValueError("reseal correction preimage invalid")
        if key == "commit" and change["after"] != source["head"]:
            raise ValueError("reseal commit differs from sealed candidate")
        if key == "pr":
            probe = current.model_copy(deep=True)
            probe.links["pr"] = change["before"]
            if change["after"] != "":
                raise ValueError("reseal only clears invalid PR bindings")
            try:
                _candidate(probe)
            except ValueError as exc:
                if str(exc) not in {"card_pr_repository_mismatch", "card_pr_binding_invalid"}:
                    raise
            else:
                raise ValueError("reseal prior PR was valid")
        prior.links[key] = change["before"]
    if card_revision(prior) != event.get("guarded_after_card_revision"):
        raise ValueError("reseal changes exceed enumerated corrections")
    repository, number, head = _candidate(current)
    expected_repo = (
        source["repository"].rstrip("/").removesuffix(".git").removeprefix("https://github.com/")
    )
    if repository != expected_repo or number is not None or head != source["head"]:
        raise ValueError("reseal current candidate differs")


def _receipt_event(store, card, receipt):
    """Require an immutable native audit record, not merely a local file."""
    matches = [
        e
        for e in store._read_events(card)
        if e.get("action") == EVENT and e.get("receipt_sha256") == _digest(receipt)
    ]
    if len(matches) != 1 or matches[0].get("writer") != ACTOR:
        raise ValueError("reseal native receipt unavailable")
    return matches[0]


def checkpoint(home, directory, context):
    """Authenticate the checkpoint and allow only exact continuation journal events."""
    directory = Path(directory)
    path = directory / "reseal.json"
    if not path.exists():
        return None
    receipt = read_json(path)
    if receipt.get("schema") != SCHEMA or receipt.get("actor") != ACTOR:
        raise ValueError("reseal receipt invalid")
    if receipt["context"] != context:
        raise ValueError("reseal context changed")
    for name, digest in receipt["files"].items():
        if Path(name).name != name or _file_hash(directory / name) != digest:
            raise ValueError("reseal original journal changed")
    store = CardStore(home)
    audit = _receipt_event(store, context["source"]["card"], receipt)
    allowed = {audit["event_id"]}
    expected = receipt["after"]
    binding = dict(read_json(directory / "finish-intent.json"), reseal_sha256=_digest(receipt))
    commands = steps(context, binding["test_receipt"])
    journal = directory / "resealed"
    pending = False
    for path in sorted(journal.glob("step-*.intent.json")):
        step = read_json(path)
        index = step["index"]
        if index not in (9, 10, 11) or pending or path.name != "step-%02d.intent.json" % index:
            raise ValueError("reseal continuation outside allowed boundary")
        if index > 9 and not (journal / ("step-%02d.ack.json" % (index - 1))).exists():
            raise ValueError("reseal continuation prefix missing")
        role, action, key, value = commands[index]
        source = context["source"]
        request = dict(
            binding=_digest(binding),
            index=index,
            role=role,
            action=action,
            key=key,
            value=value,
            before=expected,
            card=source["card"],
            owner=source["owner"],
            claim=source["claim"],
        )
        if any(step.get(k) != v for k, v in request.items()):
            raise ValueError("reseal continuation intent changed")
        events = store._read_events(source["card"])
        matches = [e for e in events if e.get("transition_id") == _digest(step)]
        if action == "complete":
            matches = [
                e
                for e in events
                if e.get("action") == "complete"
                and e["event_id"] not in receipt["source_event_ids"]
            ]
        if len(matches) > 1:
            raise ValueError("reseal ambiguous continuation")
        if matches:
            e = matches[0]
            if action == "link":
                exact = dict(
                    action="link",
                    writer=source["owner"],
                    link_key=key,
                    link_value=value,
                    expected_source_revision=expected["source"],
                    expected_claim_revision=source["claim"],
                )
                if any(e.get(k) != v for k, v in exact.items()):
                    raise ValueError("reseal continuation event changed")
            else:
                state = native_state(home, source["card"])
                before = step["native_before"]
                if (
                    e.get("writer") != source["owner"]
                    or state["status"] != "done"
                    or state["owner"] is not None
                    or state["claim_revision"] is not None
                    or state["revision"] != before["completion_revision"]
                    or state["complete_count"] != before["complete_count"] + 1
                ):
                    raise ValueError("reseal completion differs")
            allowed.add(e["event_id"])
        ack_path = path.with_name(path.name.replace(".intent.", ".ack."))
        if ack_path.exists():
            ack = read_json(ack_path)
            if (
                not matches
                or set(ack) != {"step_sha256", "after"}
                or ack["step_sha256"] != _digest(step)
                or set(ack["after"]) != set(expected)
                or ack["after"]["review"] != expected["review"]
            ):
                raise ValueError("reseal continuation acknowledgement changed")
            expected = ack["after"]
        else:
            pending = True
            if matches:
                if action == "link" and card_revision(store.fold(source["card"])) != e.get(
                    "guarded_after_card_revision"
                ):
                    raise ValueError("reseal unacknowledged write changed")
                expected = dict(expected, source=native_state(home, source["card"])["revision"])
    for role in ("source", "review"):
        if (
            history_revision(store, context[role]["card"], allowed)
            != receipt["guards"][role + "_history"]
        ):
            raise ValueError("reseal history changed")
        if native_state(home, context[role]["card"])["revision"] != expected[role]:
            raise ValueError("reseal current snapshot changed")
    return receipt


def reseal(home, card, claim, actor, changes, guards):
    """Record one exact operator checkpoint; never write PASS or complete a card."""
    from .acceptance_reseal_source import correction_view, validated_acceptance
    from .production_acceptance import review_directory

    if actor != ACTOR or set(guards) != set(GUARDS):
        raise ValueError("reseal operator and exact guards required")
    if not re.fullmatch(r"[0-9a-f]{8}", card) or not re.fullmatch(r"[0-9a-f]{32}", claim):
        raise ValueError("reseal identity invalid")
    for key, value in guards.items():
        size = 32 if key == "claim_revision" else 64
        if not re.fullmatch(r"[0-9a-f]{%d}" % size, value):
            raise ValueError("reseal guard invalid")
    home = Path(home)
    directory = review_directory(home, card, claim)
    with acceptance_lock(directory), ExitStack() as stack:
        context = read_json(directory / "context.json")
        if context["review"]["card"] != card or context["review"]["claim"] != claim:
            raise ValueError("reseal review identity differs")
        for cid in sorted({context[role]["card"] for role in ("source", "review")}):
            stack.enter_context(card_mutation_lock(home, cid))
        path = directory / "reseal.json"
        if path.exists():
            existing = read_json(path)
            if (
                existing["guards"] != guards
                or existing["changes"] != changes
                or existing["actor"] != actor
            ):
                raise ValueError("reseal replay request changed")
            # A lost reply after both durable writes is an ordinary idempotent read.
            if any(
                e.get("action") == EVENT and e.get("receipt_sha256") == _digest(existing)
                for e in CardStore(home)._read_events(context["source"]["card"])
            ):
                checkpoint(home, directory, context)
                if (
                    validated_acceptance(home, directory, context)
                    != read_json(directory / "finish-intent.json")["test_receipt"]
                ):
                    raise ValueError("reseal trusted tests changed")
                return existing
        acceptance = validated_acceptance(home, directory, context)
        step, files = _journal(directory, context, acceptance)
        store = CardStore(home)
        source, review = context["source"], context["review"]
        for item in (source, review):
            row = store.fold(item["card"])
            if (
                row.archived
                or row.meta.get("claim_conflicts")
                or {"hold", "do-not-claim"}.intersection(row.labels)
            ):
                raise ValueError("reseal card is not eligible")
        events = [
            e
            for e in store._read_events(source["card"])
            if e.get("transition_id") == _digest(step)
        ]
        if len(events) != 1:
            raise ValueError("reseal unique PASS event required")
        event = events[0]
        request = dict(
            action="link",
            writer=source["owner"],
            link_key="verdict",
            link_value="PASS",
            expected_claim_revision=source["claim"],
            expected_source_revision=step["before"]["source"],
        )
        if (
            any(event.get(k) != v for k, v in request.items())
            or _latest_outcome(store, source["card"]) != event
        ):
            raise ValueError("reseal original PASS is no longer current")
        projections = [
            e for e in _card_events(source["card"], home) if e.get("event_id") == event["event_id"]
        ]
        if len(projections) != 1 or any(
            projections[0].get(k) != event.get(k)
            for k in ("writer", "action", "link_key", "link_value", "ts", "seq")
        ):
            raise ValueError("reseal original PASS projection differs")
        source_state, review_state = (
            native_state(home, context[role]["card"]) for role in ("source", "review")
        )
        corrections = correction_view(home, source, source_state, guards["claim_revision"])
        if (
            review_state["status"] != "done"
            or review_state["owner"] is not None
            or review_state["revision"] != step["before"]["review"]
        ):
            raise ValueError("reseal custody changed")
        completion = review_state["last_complete"] or {}
        review_before = read_json(directory / "step-01.intent.json")["native_before"]
        if (
            completion.get("writer") != review["owner"]
            or review_state["complete_count"] != review_before["complete_count"] + 1
            or any(row["ts"] > completion.get("ts", "") for row in _rows(store, review["card"]))
        ):
            raise ValueError("reseal review history changed after completion")
        files.update(
            {name: _file_hash(directory / name) for name in ("context.json", "finish-intent.json")}
        )
        qualification = directory / "test-requalification.json"
        if qualification.exists():
            files[qualification.name] = _file_hash(qualification)
        actual = dict(
            context_sha256=files["context.json"],
            finish_sha256=files["finish-intent.json"],
            step_sha256=files["step-08.intent.json"],
            event_sha256=_digest(event),
            source_revision=source_state["revision"],
            review_revision=review_state["revision"],
            source_history=history_revision(store, source["card"]),
            review_history=history_revision(store, review["card"]),
            claim_revision=source["claim"],
        )
        if actual != guards:
            raise ValueError("reseal preimage changed")
        _corrections(corrections, source, event, changes, actor, set())
        receipt = dict(
            schema=SCHEMA,
            actor=actor,
            context=context,
            guards=guards,
            changes=changes,
            files=files,
            event_id=event["event_id"],
            source_event_ids=[e["event_id"] for e in store._read_events(source["card"])],
            after={"source": source_state["revision"], "review": review_state["revision"]},
        )
        if hasattr(corrections, "release"):
            receipt["released_custody"] = corrections.release
        once(path, receipt)
        store.append_event(
            source["card"],
            EVENT,
            actor,
            receipt_sha256=_digest(receipt),
            review_card=card,
            review_claim=claim,
        )
        checkpoint(home, directory, context)
        return receipt
