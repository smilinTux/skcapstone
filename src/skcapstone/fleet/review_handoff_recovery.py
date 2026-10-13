"""Recover a finished review whose guarded native handoff failed.

Incident 2026-10-13: reviewers committed a REVIEW-DECISION packet, then their
guarded handoff raised (evidence directory mode 0775) before any native verdict
was written. The dispatcher kept the dead reviewer's claim forever
(retains_review_custody) and acceptance refused "native outcome predates current
claim". Three cooperating pieces close that hole:

* authority (``recover_review_handoffs``): detect such a review from the node
  status plus native card events, write one immutable, bound recovery request
  per attempt, read the node's result, and log it;
* node (``consume`` and ``run_recovery``): run exactly the reviewer's own
  guarded handoff recipe (``production_brief.source_review_handoff_recipe``,
  the same text the worker brief embeds) as a small managed systemd unit, under
  the reviewer identity, after re-proving every binding;
* bound (``release_unrecoverable``): when the workspace or packet is gone or
  changed, stop retaining custody by releasing the dead reviewer's claim through
  the governed Board path and supersede the generation so it is re-offered.

Every write is fenced: a changed claim, source head, packet hash or binding
writes nothing and is recorded as REVIEW_HANDOFF_RECOVERY_REFUSED.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

from skcoord.card_store import CardStore

from . import builder_dispatch as dispatch
from . import production_builder, source_bundle
from .production_review_custody import unit_terminal
from .production_review_evidence import ReviewEvidenceError

REQUEST_SCHEMA = "skfleet.review-handoff-recovery-request/v1"
RESULT_SCHEMA = "skfleet.review-handoff-recovery-result/v1"
STATE = "awaiting-review-acceptance"
MAX_ATTEMPTS = 3
UNIT_RUNTIME_SECONDS = 180
CARD = re.compile(r"[0-9a-f]{8}")
CLAIM = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{64}")
HEAD = re.compile(r"[0-9a-f]{40}")

# Reasons proving the review can never be handed off. They end custody.
UNRECOVERABLE = frozenset(
    {"workspace-missing", "packet-missing", "packet-changed", "source-head-changed"}
)


def recovery_dir(paths, node: str) -> Path:
    """Authority-written requests and node-written results live side by side."""
    return paths.root / "review-handoff" / node


def _stem(card: str, claim: str, attempt: int) -> str:
    return "%s-%s-a%d" % (card, claim, attempt)


def request_file(paths, node: str, card: str, claim: str, attempt: int) -> Path:
    return recovery_dir(paths, node) / (_stem(card, claim, attempt) + ".request.json")


def result_file(paths, node: str, card: str, claim: str, attempt: int) -> Path:
    return recovery_dir(paths, node) / (_stem(card, claim, attempt) + ".result.json")


def _ack_file(paths, node: str, card: str, claim: str, attempt: int) -> Path:
    return recovery_dir(paths, node) / (_stem(card, claim, attempt) + ".ack")


def _raw(path: Path) -> bytes:
    return source_bundle._read(path, source_bundle.MAX_EVIDENCE)


def native_outcome_current(store, row) -> dict | None:
    """Return the holder's native outcome for the CURRENT claim generation, if any.

    Same binding as production_acceptance._current_outcome, but answering instead
    of raising: no outcome, a foreign writer, a mismatched claim fence or an
    outcome older than the current claim all mean "no native verdict".
    """
    from ..seraph_review_cardstore import _latest_outcome

    claim = row.meta.get("_claim_revision")
    claims = [
        event
        for event in store._read_events(row.id)
        if event.get("action") == "claim"
        and (event.get("claim_revision") or event.get("event_id")) == claim
    ]
    outcome = _latest_outcome(store, row.id)
    if (
        len(claims) != 1
        or not outcome
        or outcome.get("writer") != row.owner
        or outcome.get("expected_claim_revision") not in (None, claim)
        or str(outcome.get("ts", "")) < str(claims[0].get("ts", ""))
    ):
        return None
    return outcome


def _launch(store, card: str, owner: str, claim: str, request_id: str) -> dict | None:
    launches = [
        e
        for e in store._read_events(card)
        if e.get("action") == "review_assignment_launch"
        and e.get("schema") == "skfleet.review-assignment-launch/v3"
        and e.get("writer") == owner
        and e.get("claim_revision") == claim
        and e.get("launched") is True
        and (e.get("execution") or {}).get("request_id") == request_id
    ]
    return launches[-1] if len(launches) == 1 else None


def _is_review_card(row) -> bool:
    return "review" in {str(x).lower() for x in row.labels} and bool(
        row.meta.get("link_source_card") or row.links.get("link_source_card")
    )


def detect(paths, home: Path) -> list[dict]:
    """Find finished reviews with no native verdict for their current claim.

    Conditions (all required): node status is awaiting-review-acceptance with a
    retained review packet and a zero terminal exit; the native launch event, card
    owner and claim agree with that status; the reviewer unit is provably
    terminal; no native outcome exists for the current claim generation. A
    candidate that fails a later condition is reported with a refusal reason.
    """
    home = Path(home)
    store = CardStore(home)
    found = []
    for status_file in sorted((paths.root / "status").glob("node-*/dispatch/*.json")):
        node = status_file.parent.parent.name
        try:
            status = dispatch._validated_status(status_file, paths, node)
        except (OSError, ValueError):
            continue
        if (
            not status
            or status.get("work_kind") != "review"
            or status.get("state") != STATE
            or not isinstance(status.get("review_packet"), dict)
            or (status.get("terminal") or {}).get("exit_code") != 0
        ):
            continue
        card_id = status["card_id"]
        owner, claim = status.get("owner"), status.get("claim_revision")
        item = {"card": card_id, "node": node, "claim": claim, "owner": owner}
        try:
            row = store.fold(card_id)
            if (
                row is None
                or row.archived
                or not CARD.fullmatch(card_id)
                or not CLAIM.fullmatch(str(claim))
                or not _is_review_card(row)
            ):
                continue
            if row.owner != owner or row.meta.get("_claim_revision") != claim:
                continue  # not this claim's custody any more; nothing to recover
            if native_outcome_current(store, row) is not None:
                continue  # a native verdict exists: handoff is not the problem
            request = json.loads(_raw(dispatch.request_path(paths, node, card_id)))
            execution = status.get("execution") or {}
            if (
                request.get("request_id") != status.get("request_id")
                or request.get("reviewer") != owner
                or _launch(store, card_id, owner, claim, status["request_id"]) is None
            ):
                found.append(dict(item, refusal="generation-binding-differs"))
                continue
            try:
                unit_terminal(execution["unit"], execution["invocation"], host=execution["host"])
            except (ReviewEvidenceError, OSError, subprocess.SubprocessError, KeyError):
                found.append(dict(item, refusal="reviewer-unit-not-terminal"))
                continue
            item.update(status=status, request=request, execution=execution, row=row)
            found.append(item)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            found.append(dict(item, refusal="detection-error: " + str(exc)[:100]))
    return found


def _build_request(paths, home: Path, item: dict, attempt: int, actor: str) -> dict:
    """Bind one attempt to the exact claim, source head, packet and decision bytes."""
    from .source_bundle import load_review_packet

    status, request = item["status"], item["request"]
    card = item["card"]
    packet = load_review_packet(home, status["review_packet"], card)
    manifest = packet["manifest"]
    source = request["source"]
    if (
        manifest.get("binding", {}).get("source_head") != source["head"]
        or manifest.get("binding", {}).get("reviewer_identity") != item["owner"]
        or manifest.get("execution") != item["execution"]
    ):
        raise ValueError("packet-changed")
    return {
        "schema": REQUEST_SCHEMA,
        "card_id": card,
        "node": item["node"],
        "host": item["execution"]["host"],
        "offer_request_id": status["request_id"],
        "owner": item["owner"],
        "claim_revision": item["claim"],
        "source_card": source["card"],
        "source_head": source["head"],
        "source_tree": source["tree"],
        "packet_sha256": status["review_packet"]["sha256"],
        "review_head": manifest["review_head"],
        "decision_sha256": manifest["decision_sha256"],
        "report_sha256": manifest["report_sha256"],
        "unit": item["execution"]["unit"],
        "invocation": item["execution"]["invocation"],
        "workspace": (status.get("terminal") or {}).get("workspace"),
        "attempt": attempt,
        "actor": actor,
    }


def _results(paths, node: str, card: str, claim: str) -> dict[int, Path]:
    found = {}
    directory = recovery_dir(paths, node)
    for path in (
        directory.glob("%s-%s-a*.result.json" % (card, claim)) if directory.exists() else ()
    ):
        match = re.fullmatch(r".*-a(\d+)\.result\.json", path.name)
        if match:
            found[int(match.group(1))] = path
    return found


def _requests(paths, node: str, card: str, claim: str) -> set[int]:
    directory = recovery_dir(paths, node)
    found = set()
    for path in (
        directory.glob("%s-%s-a*.request.json" % (card, claim)) if directory.exists() else ()
    ):
        match = re.fullmatch(r".*-a(\d+)\.request\.json", path.name)
        if match:
            found.add(int(match.group(1)))
    return found


def _event(card: str, node: str, state: str, reason: str = "", **extra) -> dict:
    return dict(card=card, node=node, state=state, reason=reason, **extra)


def recover_review_handoffs(paths, home: Path, *, actor: str = "niobe") -> list[dict]:
    """Authority cycle step. Returns events the rotation log records.

    States: recovered, already-handed-off, requested, refused, released.
    The rotation log lines are REVIEW_HANDOFF_RECOVERED for the first two and
    REVIEW_HANDOFF_RECOVERY_REFUSED|reason for refused.
    """
    home = Path(home)
    events = []
    # 1. Consume node results exactly once (an .ack sidecar marks each).
    for result_path in sorted((paths.root / "review-handoff").glob("node-*/*.result.json")):
        match = re.fullmatch(
            r"([0-9a-f]{8})-([0-9a-f]{32})-a(\d+)\.result\.json", result_path.name
        )
        if not match:
            continue
        card, claim, attempt = match.group(1), match.group(2), int(match.group(3))
        node = result_path.parent.name
        ack = _ack_file(paths, node, card, claim, attempt)
        if ack.exists():
            continue
        try:
            result = json.loads(_raw(result_path))
            request_raw = _raw(request_file(paths, node, card, claim, attempt))
            if result.get("schema") != RESULT_SCHEMA or result.get(
                "request_sha256"
            ) != source_bundle._sha(request_raw):
                events.append(_event(card, node, "refused", "result-binding-differs"))
                source_bundle._once(ack, b"result-binding-differs")
                continue
            if result["state"] in {"recovered", "already-handed-off"}:
                events.append(_event(card, node, result["state"], result.get("reason", "")))
                source_bundle._once(ack, result["state"].encode())
            elif result.get("reason") in UNRECOVERABLE:
                events.append(
                    release_unrecoverable(
                        paths, home, node, card, claim, result["reason"], result_path, actor
                    )
                )
                if events[-1]["state"] == "released":
                    source_bundle._once(ack, b"released")
            else:
                events.append(_event(card, node, "refused", result.get("reason", "unknown")))
                source_bundle._once(ack, b"refused")
        except (OSError, ValueError, KeyError, TypeError, source_bundle.SourceBundleError) as exc:
            events.append(_event(card, node, "refused", "result-unreadable: " + str(exc)[:100]))
    # 2. Detect finished reviews still lacking a native verdict; request recovery.
    for item in detect(paths, home):
        card, node, claim = item["card"], item["node"], item["claim"]
        if "refusal" in item:
            events.append(_event(card, node, "refused", item["refusal"]))
            continue
        issued, done = _requests(paths, node, card, claim), _results(paths, node, card, claim)
        if issued - set(done):
            continue  # an attempt is in flight on the node
        attempt = max(issued, default=0) + 1
        try:
            if attempt > MAX_ATTEMPTS:
                events.append(_event(card, node, "refused", "attempts-exhausted"))
                continue
            request = _build_request(paths, home, item, attempt, actor)
            source_bundle._once(
                request_file(paths, node, card, claim, attempt),
                json.dumps(request, sort_keys=True).encode(),
            )
            events.append(_event(card, node, "requested", "attempt %d" % attempt))
        except (OSError, source_bundle.SourceBundleError):
            events.append(
                release_unrecoverable(
                    paths, home, node, card, claim, "packet-missing", None, actor
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            if str(exc) == "packet-changed":
                events.append(
                    release_unrecoverable(
                        paths, home, node, card, claim, "packet-changed", None, actor
                    )
                )
            else:
                events.append(_event(card, node, "refused", str(exc)[:100]))
    return events


def release_unrecoverable(
    paths, home: Path, node: str, card: str, claim: str, reason: str, evidence, actor: str
) -> dict:
    """Stop retaining custody: release the dead reviewer's own claim, re-offer.

    Fenced to the exact review generation. Refuses a source card, a claim that is
    not the sealed reviewer's, finished work (the PASS guard of #1169) or any
    started acceptance. Never touches the source card.
    """
    from ..coordination import Board
    from ..release_guard import finished_work_verdict
    from . import review_retire
    from .production_acceptance import review_directory

    home = Path(home)
    try:
        store = CardStore(home)
        row = store.fold(card)
        status = json.loads(_raw(dispatch.status_path(paths, node, card)))
        owner = status.get("owner")
        if (
            row is None
            or not _is_review_card(row)
            or row.id == (row.meta.get("link_source_card") or row.links.get("link_source_card"))
            or status.get("work_kind") != "review"
            or status.get("claim_revision") != claim
            or status.get("state") != STATE
        ):
            return _event(card, node, "refused", "release-fence-differs")
        directory = review_directory(home, card, claim)
        if any((directory / n).exists() for n in ("finished.json", "finish-intent.json")):
            return _event(card, node, "refused", "acceptance-started")
        if row.owner is not None:
            if row.owner != owner or row.meta.get("_claim_revision") != claim:
                return _event(card, node, "refused", "claim-changed")
            if native_outcome_current(store, row) is not None or finished_work_verdict(
                home, card, owner, claim
            ):
                return _event(card, node, "refused", "holds-finished-work")
            Board(home).release_claim(
                owner, card, actor=owner, expected_claim_revision=claim, abandon_reason="error"
            )
        row = store.fold(card)
        if row is None or row.owner is not None or row.meta.get("_claim_revision"):
            return _event(card, node, "refused", "release-readback-differs")
        if getattr(row.status, "value", row.status) != "review":
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "skcapstone",
                    "coord",
                    "move",
                    card,
                    "review",
                    "--agent",
                    actor,
                    "--home",
                    str(home),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        context = (
            _raw(evidence)
            if evidence is not None
            else json.dumps(
                {"card": card, "claim": claim, "reason": reason}, sort_keys=True
            ).encode()
        )
        outcome = review_retire.supersede_generation(
            paths,
            home,
            card,
            claim,
            owner,
            actor=actor,
            reason=SUPERSEDE_REASON,
            raw_context=context,
        )
        if outcome != "superseded":
            return _event(card, node, "refused", "supersede-" + outcome)
        return _event(card, node, "released", reason)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return _event(card, node, "refused", "release-error: " + str(exc)[:100])


SUPERSEDE_REASON = (
    "automatic: finished review could not complete its guarded handoff and its "
    "workspace or packet is gone or changed; dead reviewer claim released so the "
    "review is re-offered"
)


# ---------------------------------------------------------------- node side


def _refuse(reason: str, detail: str = "") -> dict:
    return {"state": "refused", "reason": reason, "detail": detail[:300]}


def _verify(paths, home: Path, node: str, request: dict, store) -> tuple[dict | None, dict | None]:
    """Re-prove every binding; return (refusal, context). Writes nothing."""
    from ..review_admission import reviewer_candidate_reasons  # noqa: F401
    from .production_brief import source_review_binding
    from .source_bundle import SourceBundleError, inspect_remote_review

    card, owner, claim = request["card_id"], request["owner"], request["claim_revision"]
    row = store.fold(card)
    if (
        row is None
        or row.archived
        or row.owner != owner
        or row.meta.get("_claim_revision") != claim
        or row.meta.get("claim_conflicts")
    ):
        return _refuse("claim-changed"), None
    if native_outcome_current(store, row) is not None:
        return {"state": "already-handed-off", "reason": "native verdict present"}, None
    try:
        status = json.loads(_raw(dispatch.status_path(paths, node, card)))
    except (OSError, ValueError):
        return _refuse("packet-missing", "node status unreadable"), None
    reference = status.get("review_packet") or {}
    if (
        status.get("state") != STATE
        or status.get("claim_revision") != claim
        or status.get("request_id") != request["offer_request_id"]
        or reference.get("sha256") != request["packet_sha256"]
    ):
        return _refuse("packet-changed", "status or packet reference differs"), None
    core = row.model_dump(mode="json")
    try:
        expected, bindings = source_review_binding(
            card_id=card,
            owner=owner,
            claim_revision=claim,
            source_head=request["source_head"],
            core=core,
            labels=row.labels,
        )
    except ValueError as exc:
        head = row.meta.get("link_head_revision") or row.links.get("link_head_revision")
        if head and head != request["source_head"]:
            return _refuse("source-head-changed", str(exc)), None
        return _refuse("binding-changed", str(exc)), None
    workspace = Path(str(request.get("workspace") or ""))
    if (
        not workspace.is_absolute()
        or workspace != paths.root / "workspaces" / owner
        or not workspace.is_dir()
        or not (workspace / ".git").is_dir()
        or (workspace / ".git").is_symlink()
    ):
        return _refuse("workspace-missing", str(workspace)), None
    try:
        proposal = inspect_remote_review(
            workspace,
            card=card,
            parent_card=request["source_card"],
            source_head=request["source_head"],
            source_tree=request["source_tree"],
            reviewer_identity=owner,
        )
    except (SourceBundleError, ReviewEvidenceError, OSError, ValueError) as exc:
        return _refuse("packet-missing", "committed decision unavailable: " + str(exc)), None
    if (
        proposal["review_head"] != request["review_head"]
        or proposal["decision_sha256"] != request["decision_sha256"]
        or proposal["report_sha256"] != request["report_sha256"]
    ):
        return _refuse("packet-changed", "committed decision differs from request"), None
    return None, {
        "expected": expected,
        "bindings": bindings,
        "workspace": workspace,
        "tree": request["source_tree"],
    }


def run_recovery(paths, home: Path, node: str, request_path: Path) -> dict:
    """Run on the reviewer host: fenced, idempotent re-run of the guarded handoff."""
    from ..release_guard import finished_work_verdict  # noqa: F401
    from .production_brief import source_review_handoff_recipe
    from .worker_git import identity

    home = Path(home)
    raw = _raw(request_path)
    request = json.loads(raw)
    result_path = result_file(
        paths, node, request["card_id"], request["claim_revision"], request["attempt"]
    )
    base = {"schema": RESULT_SCHEMA, "request_sha256": source_bundle._sha(raw)}
    if result_path.exists():
        # Idempotent: one attempt writes one immutable result, never twice.
        return json.loads(_raw(result_path))

    def finish(value: dict) -> dict:
        value = dict(base, **value, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        source_bundle._once(result_path, json.dumps(value, sort_keys=True).encode())
        return value

    try:
        host = socket.gethostname().split(".")[0].lower()
        if (
            request.get("schema") != REQUEST_SCHEMA
            or request.get("node") != node
            or request.get("host") != host
            or not CARD.fullmatch(request.get("card_id", ""))
            or not CLAIM.fullmatch(request.get("claim_revision", ""))
            or not HEAD.fullmatch(request.get("source_head", ""))
            or not SHA.fullmatch(request.get("packet_sha256", ""))
        ):
            return finish(_refuse("request-changed", "request binding or host differs"))
        store = CardStore(home)
        refusal, context = _verify(paths, home, node, request, store)
        if refusal is not None:
            return finish(refusal)
        owner, card = request["owner"], request["card_id"]
        recipe = source_review_handoff_recipe(
            card_id=card,
            owner=owner,
            claim_revision=request["claim_revision"],
            source_head=request["source_head"],
            tree=context["tree"],
            expected=context["expected"],
            bindings=context["bindings"],
        )
        env = {
            "HOME": str(Path.home()),
            "PATH": production_builder.review_worker_path(
                Path.home(), os.environ.get("PATH", "/usr/bin:/bin")
            ),
            "LANG": "C.UTF-8",
            "SKCAPSTONE_HOME": str(home),
            "SKAGENT": owner,
            "SKCAPSTONE_AGENT": owner,
            "SKFLEET_CARD_ID": card,
            "SKFLEET_CLAIM_REVISION": request["claim_revision"],
            "SKFLEET_SESSION_ID": owner,
            "SKFLEET_WORKSPACE": str(context["workspace"]),
            **identity(owner),
        }
        done = subprocess.run(
            ["/bin/bash", "-c", recipe],
            cwd=context["workspace"],
            env=env,
            capture_output=True,
            text=True,
            timeout=UNIT_RUNTIME_SECONDS - 20,
        )
        if done.returncode:
            tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or [""]
            return finish(_refuse("handoff-failed", tail[0]))
        row = store.fold(card)
        if row is None or native_outcome_current(store, row) is None:
            return finish(_refuse("readback-missing", "no native verdict after handoff"))
        return finish({"state": "recovered", "reason": "guarded handoff completed"})
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        try:
            return finish(_refuse("handoff-failed", "%s: %s" % (type(exc).__name__, exc)))
        except (OSError, source_bundle.SourceBundleError):
            return _refuse("handoff-failed", str(exc))


def _launch_unit(command: list[str]) -> int:
    return subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True, timeout=UNIT_RUNTIME_SECONDS + 30
    ).returncode


def consume(paths, home: Path, node: str, *, launcher=None) -> list[dict]:
    """Node loop step: run each pending request as a small managed unit.

    The unit re-proves every fence itself (``run_recovery``); the node only
    starts it and, if the unit died without a result, records that refusal.
    """
    home = Path(home)
    launcher = launcher or _launch_unit
    outcomes = []
    directory = recovery_dir(paths, node)
    for path in sorted(directory.glob("*.request.json")) if directory.exists() else ():
        match = re.fullmatch(r"([0-9a-f]{8})-([0-9a-f]{32})-a(\d+)\.request\.json", path.name)
        if not match:
            continue
        card, claim, attempt = match.group(1), match.group(2), int(match.group(3))
        target = result_file(paths, node, card, claim, attempt)
        with dispatch._request_exclusion(path):
            if target.exists():
                continue
            raw = _raw(path)
            try:
                host = json.loads(raw).get("host")
            except ValueError:
                continue
            if host != socket.gethostname().split(".")[0].lower():
                continue  # another host's node shares this name only by mistake
            unit = "skfleet-handoff-recover-%s-%s.service" % (card, claim[:8] + "a%d" % attempt)
            command = [
                "/usr/bin/systemd-run",
                "--user",
                "--quiet",
                "--wait",
                "--collect",
                "--unit=" + unit,
                "--property=UMask=0077",
                "--property=NoNewPrivileges=yes",
                "--property=KillMode=control-group",
                "--property=RuntimeMaxSec=%d" % UNIT_RUNTIME_SECONDS,
                "--",
                sys.executable,
                "-m",
                "skcapstone.fleet.review_handoff_recovery",
                "run",
                "--fleet-root",
                str(paths.root),
                "--home",
                str(home),
                "--node",
                node,
                "--request",
                str(path),
            ]
            code = launcher(command)
            if not target.exists():
                value = dict(
                    schema=RESULT_SCHEMA,
                    request_sha256=source_bundle._sha(raw),
                    state="refused",
                    reason="handoff-failed",
                    detail="recovery unit exit %s without result" % code,
                    ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                source_bundle._once(target, json.dumps(value, sort_keys=True).encode())
            outcomes.append({"card": card, "attempt": attempt, "exit": code})
    return outcomes


def main(argv=None) -> int:
    """Entry for the managed recovery unit: ``... review_handoff_recovery run ...``."""
    import argparse

    from .paths import FleetPaths

    parser = argparse.ArgumentParser(prog="review_handoff_recovery")
    parser.add_argument("action", choices=["run"])
    parser.add_argument("--fleet-root", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    result = run_recovery(
        FleetPaths(Path(args.fleet_root)), Path(args.home), args.node, Path(args.request)
    )
    print(shlex.quote(json.dumps(result, sort_keys=True)))
    return 0 if result.get("state") in {"recovered", "already-handed-off"} else 1


if __name__ == "__main__":
    sys.exit(main())
