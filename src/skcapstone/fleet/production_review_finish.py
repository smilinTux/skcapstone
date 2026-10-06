"""Crash-safe source acceptance through the authority's guarded native CLI.

The guarded authority CLI is an explicit deployment dependency. Worker model
text never authorizes a completion; a committed proposal and qualified native
test receipt are independently revalidated before every mutation.
"""

import json
import subprocess
import sys
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from ..blocked_verdict import is_outcome_key
from ..seraph_review_cardstore import LiveCardStoreGateway, card_revision
from ..seraph_review_contracts import _digest
from .production_review_evidence import ReviewEvidenceError
from .source_bundle import MAX_EVIDENCE, _once, _read, _sha


def read_json(path):
    """Read only bounded, private, owned, regular evidence."""
    return json.loads(_read(Path(path), MAX_EVIDENCE))


def once(path, value):
    """Retain complete immutable structured evidence."""
    _once(Path(path), json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def native_state(home, card):
    """Read exact current custody and predict native completion for lost replies."""
    with card_mutation_lock(home, card):
        store = CardStore(home)
        row = store.fold(card)
        if row is None:
            raise ReviewEvidenceError("native card disappeared")
        snapshot = LiveCardStoreGateway(home).read_card(card, _store=store)
        after = row.model_copy(deep=True)
        after.status = type(row.status)("done")
        after.owner = None
        after.meta.pop("_claim_revision", None)
        bindings = {
            key: getattr(snapshot, key)
            for key in (
                "repository",
                "number",
                "head_sha",
                "producer_identity",
                "reviewer_identity",
                "unresolved_review",
                "candidate_evidence_sha256",
            )
        }
        native = store._read_events(card)
        events = native + store._legacy_events(card)
        events.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)))
        outcomes = [
            e
            for e in events
            if e.get("action") == "verdict"
            or (e.get("action") == "link" and is_outcome_key(e.get("link_key") or e.get("key")))
        ]
        predicted = _digest(
            {
                "card": card_revision(after),
                "bindings": bindings,
                "outcome": outcomes[-1] if outcomes else {},
            }
        )
        completions = [e for e in native if e.get("action") == "complete"]
        return {
            "revision": snapshot.revision,
            "completion_revision": predicted,
            "complete_count": len(completions),
            "last_complete": completions[-1] if completions else None,
            "owner": row.owner,
            "claim_revision": row.meta.get("_claim_revision"),
            "status": row.status.value,
        }


def native_command(home, args):
    """Use this interpreter's native CLI with no unguarded fallback."""
    result = subprocess.run(
        [sys.executable, "-m", "skcapstone", "coord", *args, "--home", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise ReviewEvidenceError("guarded native acceptance refused")
    return json.loads(result.stdout) if "--json" in args else None


def artifacts(context):
    """Rehash exact collected source evidence and independent decision bytes."""
    for role in ("source", "review"):
        item = context[role]
        if _sha(_read(Path(item["evidence_path"]), MAX_EVIDENCE)) != item["evidence_sha256"]:
            raise ReviewEvidenceError("retained report changed")
    review = context["review"]
    raw = _read(Path(review["decision_path"]), MAX_EVIDENCE)
    if (
        _sha(raw) != review["decision_sha256"]
        or json.loads(raw) != review["proposal"]
        or review["proposal"]["verdict"] != "PASS"
    ):
        raise ReviewEvidenceError("independent review is not exact PASS")


def steps(context, acceptance):
    """Complete reviewer first; source completion joins its exact final revision."""
    source, review = context["source"], context["review"]
    audit = json.dumps(acceptance, sort_keys=True, separators=(",", ":"))
    # The reviewer has already sealed exactly one applicability receipt after
    # its report and verdict. Reissuing those links would invalidate its order
    # or create a duplicate receipt. Add only independent controller test proof.
    values = [("test_acceptance", audit)]
    result = [("review", "link", key, value) for key, value in values]
    result.append(("review", "complete", "", ""))
    repo = source["repository"].rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    values = [
        ("review_join", review["card"]),
        ("test_acceptance", audit),
        ("commit_sha", source["head"]),
        ("branch", repo + ":" + source["ref"].removeprefix("refs/heads/")),
        ("head", source["head"]),
        ("candidate_evidence_sha256", source["evidence_sha256"]),
        ("verdict", "PASS"),
        ("evidence", source["evidence_path"]),
        ("evidence_sha256", source["evidence_sha256"]),
    ]
    result.extend(("source", "link", key, value) for key, value in values)
    result.append(("source", "complete", "", ""))
    return result


def _historical_acceptance(home, context, historical, inspect):
    """Read immutable accepted proof anchored in both completed native cards."""
    from . import production_test_composite as composite
    from .production_test_plan import (
        approved_checks,
        check_binding,
        read_private,
        sha,
    )
    from .production_test_profile import recipe_checks

    acceptance = historical.get("test_receipt")
    binding = context["test_binding"]
    check_binding(binding)
    if (
        historical.get("accepted") is not True
        or not isinstance(acceptance, dict)
        or historical.get("context_sha256") != _digest(context)
        or any(
            context["source"].get(key) != binding[field]
            for key, field in (
                ("card", "source_card"),
                ("head", "source_head"),
                ("tree", "source_tree"),
                ("owner", "source_owner"),
                ("claim", "source_claim_revision"),
            )
        )
    ):
        raise ReviewEvidenceError("historical acceptance context changed")
    store = CardStore(home)
    for role in ("source", "review"):
        card = context[role]["card"]
        state, row = inspect(home, card), store.fold(card)
        if (
            state["revision"] != historical.get("revisions", {}).get(role)
            or state["status"] != "done"
            or state["owner"] is not None
            or state["claim_revision"] is not None
            or row is None
            or json.loads(row.links.get("test_acceptance", "null")) != acceptance
        ):
            raise ReviewEvidenceError("historical acceptance lacks exact native completion")
    plan_path = (
        Path(home)
        / "fleet/test-plans"
        / (binding["source_card"] + "-" + binding["source_head"] + ".json")
    )
    raw_plan = read_private(plan_path)
    plan_sha = sha(raw_plan)
    if acceptance.get("plan_sha256") != plan_sha:
        raise ReviewEvidenceError("retained historical test plan changed")
    plan = json.loads(raw_plan)
    directory = Path(home) / "fleet/test-runs" / plan_sha
    receipt_path = directory / "receipt.json"
    raw_receipt = read_private(receipt_path)
    receipt = json.loads(raw_receipt)
    if (
        acceptance.get("receipt_path") != str(receipt_path)
        or acceptance.get("receipt_sha256") != sha(raw_receipt)
        or acceptance.get("source_head") != binding["source_head"]
        or plan.get("binding") != binding
        or receipt.get("binding") != binding
        or receipt.get("plan_sha256") != plan_sha
        or receipt.get("checks") != acceptance.get("checks")
    ):
        raise ReviewEvidenceError("retained historical test proof changed")
    profile = plan.get("profile")
    expected = recipe_checks(profile["recipe"]) if profile is not None else approved_checks()
    results = receipt.get("checks")
    if (
        plan.get("checks") != expected
        or not isinstance(results, list)
        or len(results) != len(expected)
    ):
        raise ReviewEvidenceError("historical required checks changed")
    for required, check in zip(expected, results):
        # Derive IDs from fixed templates, never a local path supplied by history.
        if (
            check.get("id") != required["id"]
            or check.get("argv") != required["argv"]
            or type(check.get("exit_code")) is not int
            or check["exit_code"] != 0
            or sha(read_private(directory / (check["id"] + ".log"))) != check.get("output_sha256")
        ):
            raise ReviewEvidenceError("historical raw test output changed")
    reports = {name: read_private(directory / name) for name in composite.report_names(profile)}
    selection = None
    if composite.python_selection(profile):
        selection_raw = read_private(directory / "selection.json")
        if receipt.get("selection_sha256") != sha(selection_raw):
            raise ReviewEvidenceError("historical selection proof changed")
        selection = json.loads(selection_raw)
    report_digest, counts = composite.evidence(reports, profile, selection)
    if (
        receipt.get("junit_sha256") != report_digest
        or receipt.get("counts") != counts
        or acceptance.get("counts") != counts
    ):
        raise ReviewEvidenceError("historical JUnit proof changed")
    return acceptance


def finish_pair(home, directory, context, *, guard, command=native_command, inspect=native_state):
    """Finish an exact tested pair idempotently, recovering even lost CLI replies."""
    from .production_tests import validate_test_receipt

    directory = Path(directory)
    artifacts(context)
    finished = directory / "finished.json"
    historical = read_json(finished) if finished.exists() else None
    acceptance = (
        _historical_acceptance(home, context, historical, inspect)
        if historical is not None
        else validate_test_receipt(
            home, context["test_binding"], Path(context["source_workspace"])
        )
    )
    binding = {
        "controller": context["controller"],
        "context_sha256": _digest(context),
        "test_receipt": acceptance,
    }
    initial = {name: context[name]["revision"] for name in ("source", "review")}
    intent = directory / "finish-intent.json"
    if not intent.exists():
        if historical is not None:
            raise ReviewEvidenceError("historical acceptance intent missing")
        guard()
        if any(
            inspect(home, context[name]["card"])["revision"] != initial[name] for name in initial
        ):
            raise ReviewEvidenceError("acceptance generation changed")
    if historical is not None:
        if read_json(intent) != binding:
            raise ReviewEvidenceError("historical acceptance intent changed")
    else:
        once(intent, binding)
    expected = initial
    for index, (role, action, key, value) in enumerate(steps(context, acceptance)):
        item = context[role]
        other = "review" if role == "source" else "source"
        request = {
            "binding": _digest(binding),
            "index": index,
            "role": role,
            "action": action,
            "key": key,
            "value": value,
            "before": expected,
            "card": item["card"],
            "owner": item["owner"],
            "claim": item["claim"],
        }
        step_path = directory / ("step-%02d.intent.json" % index)
        ack_path = directory / ("step-%02d.ack.json" % index)
        if step_path.exists():
            step = read_json(step_path)
            if any(step.get(name) != val for name, val in request.items()):
                raise ReviewEvidenceError("acceptance step changed")
        else:
            if historical:
                raise ReviewEvidenceError("acceptance history incomplete")
            guard()
            before = inspect(home, item["card"])
            if (
                before["revision"] != expected[role]
                or before["owner"] != item["owner"]
                or before["claim_revision"] != item["claim"]
            ):
                raise ReviewEvidenceError("acceptance claim or source changed")
            step = dict(request, native_before=before)
            once(step_path, step)
        if ack_path.exists():
            ack = read_json(ack_path)
            if (
                set(ack) != {"step_sha256", "after"}
                or ack["step_sha256"] != _digest(step)
                or set(ack["after"]) != set(expected)
                or ack["after"][other] != expected[other]
            ):
                raise ReviewEvidenceError("acceptance acknowledgement changed")
            expected = ack["after"]
            continue
        if historical:
            raise ReviewEvidenceError("acceptance acknowledgement missing")
        guard()
        artifacts(context)
        if (
            validate_test_receipt(home, context["test_binding"], Path(context["source_workspace"]))
            != acceptance
        ):
            raise ReviewEvidenceError("trusted test evidence changed")
        if inspect(home, context[other]["card"])["revision"] != expected[other]:
            raise ReviewEvidenceError("related native generation changed")
        args = [action, item["card"]]
        if action == "link":
            args += [key, value, "--transition-id", _digest(step), "--json"]
        args += [
            "--agent",
            item["owner"],
            "--expected-source-revision",
            expected[role],
            "--expected-claim-revision",
            item["claim"],
        ]
        if action == "complete" and role == "source":
            args += [
                "--review-card",
                context["review"]["card"],
                "--expected-review-revision",
                expected["review"],
            ]
        if action == "link":
            reply = command(home, args)
            after = inspect(home, item["card"])
            if (
                reply.get("card_id") != item["card"]
                or reply.get("source_revision") != after["revision"]
            ):
                raise ReviewEvidenceError("guarded link readback differs")
        else:
            after = inspect(home, item["card"])
            if after["revision"] == expected[role]:
                command(home, args)
                after = inspect(home, item["card"])
            last = after["last_complete"] or {}
            before = step["native_before"]
            if (
                after["revision"] != before["completion_revision"]
                or after["status"] != "done"
                or after["owner"] is not None
                or after["claim_revision"] is not None
                or last.get("writer") != item["owner"]
                or after["complete_count"] != before["complete_count"] + 1
            ):
                raise ReviewEvidenceError("native completion readback differs")
        if inspect(home, context[other]["card"])["revision"] != expected[other]:
            raise ReviewEvidenceError("related native generation changed during write")
        expected = dict(expected, **{role: after["revision"]})
        once(ack_path, {"step_sha256": _digest(step), "after": expected})
    if any(
        inspect(home, context[name]["card"])["revision"] != expected[name] for name in expected
    ):
        raise ReviewEvidenceError("completed native pair changed")
    result = {
        "schema": "skfleet.source-review-acceptance/v1",
        "controller": context["controller"],
        "context_sha256": _digest(context),
        "revisions": expected,
        "source_card": context["source"]["card"],
        "review_card": context["review"]["card"],
        "accepted": True,
        "test_receipt": acceptance,
        "governed_pr_ci": False,
    }
    if historical is not None:
        if historical != result:
            raise ReviewEvidenceError("historical acceptance result changed")
    else:
        once(finished, result)
    return result
