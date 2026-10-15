"""Crash-safe acceptance via guarded CLI, revalidating review and trusted test evidence."""

import json
import subprocess
import sys
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from ..blocked_verdict import is_outcome_key
from ..seraph_review_cardstore import LiveCardStoreGateway, card_revision
from ..seraph_review_contracts import _digest
from .production_review_evidence import ReviewEvidenceError
from .production_review_steps import steps  # Re-export for historical recovery callers.
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
        events.sort(
            key=lambda e: (
                e.get("ts", ""),
                e.get("writer", ""),
                e.get("seq", 0),
                bool(e.get("expected_claim_revision")),
            )
        )
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
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = ": " + detail[-1][:180] if detail else ""
        raise ReviewEvidenceError("guarded native acceptance refused" + suffix)
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
    from .retained_test_plan import read_accepted_plan

    raw_plan = read_accepted_plan(home, binding, acceptance.get("plan_sha256"))
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


def _finish_test_receipt(directory, context, current):
    """Keep an existing finish journal immutable after trusted requalification."""
    intent_path = Path(directory) / "finish-intent.json"
    if not intent_path.exists():
        return current
    intent = read_json(intent_path)
    prior = intent.get("test_receipt")
    if prior == current:
        return current
    recovery_path = Path(directory) / "test-requalification.json"
    if not recovery_path.exists():
        raise ReviewEvidenceError("historical acceptance intent changed")
    recovery = read_json(recovery_path)
    expected = {
        "schema": "skfleet.acceptance-test-requalification/v1",
        "context_sha256": _digest(context),
        "finish_intent_sha256": _digest(intent),
        "prior_test_receipt": prior,
        "current_test_receipt": current,
    }
    if recovery != expected:
        raise ReviewEvidenceError("acceptance test requalification changed")
    return prior


def finish_pair(
    home,
    directory,
    context,
    *,
    guard,
    command=native_command,
    inspect=native_state,
    review_initial_revision=None,
):
    """Finish an exact tested pair idempotently, recovering even lost CLI replies."""
    from .acceptance_reseal import acceptance_lock

    if (Path(directory) / "finished.json").exists():
        # Completed evidence is immutable: validate it without creating a lock file.
        return _finish_pair(
            home,
            directory,
            context,
            guard=guard,
            command=command,
            inspect=inspect,
            historical_only=True,
            review_initial_revision=review_initial_revision,
        )
    with acceptance_lock(directory):
        return _finish_pair(
            home,
            directory,
            context,
            guard=guard,
            command=command,
            inspect=inspect,
            review_initial_revision=review_initial_revision,
        )


def _finish_pair(
    home,
    directory,
    context,
    *,
    guard,
    command,
    inspect,
    historical_only=False,
    review_initial_revision=None,
):
    """Run the existing journal, or an explicitly authenticated step-08 checkpoint."""
    from .acceptance_reseal import checkpoint
    from .production_tests import validate_test_receipt
    from .source_custody import CustodyState, inspect_custody

    directory = Path(directory)
    source = context["source"]
    recovery = checkpoint(home, directory, context)
    journal = directory / "resealed" if recovery else directory
    artifacts(context)
    finished = directory / "finished.json"
    historical = read_json(finished) if historical_only or finished.exists() else None
    if historical_only and not isinstance(historical, dict):
        raise ReviewEvidenceError("completed acceptance marker changed")
    acceptance = (
        _historical_acceptance(home, context, historical, inspect)
        if historical is not None
        else validate_test_receipt(
            home, context["test_binding"], Path(context["source_workspace"])
        )
    )
    if historical is None:
        acceptance = _finish_test_receipt(directory, context, acceptance)
    refresh_path = directory / "review-pair-refresh.json"
    if review_initial_revision is None:
        if refresh_path.exists():
            refresh = read_json(refresh_path)
            review_initial_revision = refresh.get("review_initial_revision")
        else:
            review_initial_revision = context["review"]["revision"]
    binding_base = {
        "controller": context["controller"],
        "context_sha256": _digest(context),
        "test_receipt": acceptance,
    }
    if review_initial_revision != context["review"]["revision"]:
        if not refresh_path.exists():
            raise ReviewEvidenceError("review pair refresh missing")
        refresh = read_json(refresh_path)
        if refresh.get("review_initial_revision") != review_initial_revision:
            raise ReviewEvidenceError("review pair refresh changed")
        binding_base["review_initial_revision"] = review_initial_revision
        binding_base["review_pair_refresh_sha256"] = _digest(refresh)
    source_before = inspect(home, source["card"])
    custody = inspect_custody(
        home,
        source["card"],
        source["owner"],
        source["claim"],
        held_statuses=frozenset({source_before["status"]}),
        source=source,
        revision=source_before["revision"],
    )
    source_claim_current = custody.state == CustodyState.HELD
    released_source = custody.state == CustodyState.RELEASED_CLEAN
    if not source_claim_current and not released_source and source_before["status"] != "done":
        raise ReviewEvidenceError("source claim changed before review acceptance")
    intent = directory / "finish-intent.json"
    if intent.exists():
        prior_binding = read_json(intent)
        if any(prior_binding.get(key) != value for key, value in binding_base.items()):
            raise ReviewEvidenceError("historical acceptance intent changed")
        source_claim_current = prior_binding.get("source_claim_current", True)
        binding = dict(binding_base)
        if "source_claim_current" in prior_binding:
            binding["source_claim_current"] = source_claim_current
        if "released_source_revision" in prior_binding:
            binding["released_source_revision"] = prior_binding["released_source_revision"]
        if "source_recipe_revision" in prior_binding:
            binding["source_recipe_revision"] = prior_binding["source_recipe_revision"]
        if "review_revision" in prior_binding:
            binding["review_revision"] = prior_binding["review_revision"]
        if prior_binding != binding:
            raise ReviewEvidenceError("historical acceptance intent changed")
    else:
        # This historical journal flag selects the full producer-link sequence.
        source_claim_current = source_claim_current or released_source
        binding = {**binding_base, "source_claim_current": source_claim_current}
        if released_source:
            binding["released_source_revision"] = source_before["revision"]
        if inspect(home, context["review"]["card"])["revision"] != context["review"]["revision"]:
            from .acceptance_revision import review_start_revision

            binding["review_revision"] = review_start_revision(home, context["review"])
    initial = {
        "source": binding.get(
            "released_source_revision",
            binding.get("source_recipe_revision", source.get("live_revision", source["revision"])),
        ),
        "review": binding.get(
            "review_revision",
            binding.get("review_initial_revision", context["review"]["revision"]),
        ),
    }
    if not intent.exists():
        if historical is not None:
            raise ReviewEvidenceError("historical acceptance intent missing")
        guard()
        current_source_revision = inspect(home, source["card"])["revision"]
        if (
            not released_source
            and current_source_revision != initial["source"]
            and "source_recipe_revision" not in binding
        ):
            from .acceptance_revision import source_recipe_revision

            binding["source_recipe_revision"] = source_recipe_revision(home, source)
            initial["source"] = binding["source_recipe_revision"]
        if any(
            inspect(home, context[name]["card"])["revision"] != initial[name] for name in initial
        ):
            raise ReviewEvidenceError("acceptance generation changed")
    if historical is not None:
        if read_json(intent) != binding:
            raise ReviewEvidenceError("historical acceptance intent changed")
    else:
        once(intent, binding)
    if recovery:
        binding = dict(binding, reseal_sha256=_digest(recovery))
    expected = recovery["after"] if recovery else initial
    for index, (role, action, key, value) in enumerate(
        steps(context, acceptance, source_claim_current=source_claim_current)
    ):
        if recovery and index < 9:
            continue
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
            "claim": item["claim"] if role != "source" or source_claim_current else None,
        }
        step_path = journal / ("step-%02d.intent.json" % index)
        ack_path = journal / ("step-%02d.ack.json" % index)
        if step_path.exists():
            step = read_json(step_path)
            if any(step.get(name) != val for name, val in request.items()):
                raise ReviewEvidenceError("acceptance step changed")
        else:
            if historical:
                raise ReviewEvidenceError("acceptance history incomplete")
            guard()
            before = inspect(home, item["card"])
            current_owner = (
                before["owner"] == item["owner"] and before["claim_revision"] == item["claim"]
            )
            released_owner = role == "source" and released_source
            if released_owner:
                from ..guarded_released_source import released_revision_guard

                released_revision_guard(
                    home,
                    item["owner"],
                    item["card"],
                    expected[role],
                    item["claim"],
                    json.dumps(source),
                )()
            elif before["revision"] != expected[role] or not current_owner:
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
        if recovery:
            checkpoint(home, directory, context)
        artifacts(context)
        current_acceptance = validate_test_receipt(
            home, context["test_binding"], Path(context["source_workspace"])
        )
        if _finish_test_receipt(directory, context, current_acceptance) != acceptance:
            raise ReviewEvidenceError("trusted test evidence changed")
        if inspect(home, context[other]["card"])["revision"] != expected[other]:
            raise ReviewEvidenceError("related native generation changed")
        args = [action, item["card"]]
        if action == "link":
            args += [key, value, "--transition-id", _digest(step), "--json"]
        args += ["--agent", item["owner"], "--expected-source-revision", expected[role]]
        if role != "source" or source_claim_current or released_source:
            args += ["--expected-claim-revision", item["claim"]]
        if role == "source" and released_source:
            args += ["--released-source-binding", json.dumps(source, sort_keys=True)]
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
            if after["revision"] == expected[role] or (
                role == "source" and released_source and after["status"] != "done"
            ):
                # The native guard proves a pending completion across release.
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
    if recovery:
        checkpoint(home, directory, context)
    result = {
        "schema": "skfleet.source-review-acceptance/v1",
        "controller": context["controller"],
        "context_sha256": _digest(context),
        "revisions": expected,
        "source_card": source["card"],
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
