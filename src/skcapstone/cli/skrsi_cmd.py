"""Card-authorized SKRSI metadata runtime entrypoint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import click


def register_skrsi_commands(main: click.Group) -> None:
    """Register bounded first-wave runtime work and immutable receipt reads."""

    @main.group("skrsi")
    def skrsi():
        """Run governed metadata handoffs. No merge, deployment, or actuation."""

    @skrsi.command("run")
    @click.option("--home", type=click.Path(path_type=Path), required=True)
    @click.option(
        "--state-dir",
        type=click.Path(path_type=Path),
        required=True,
        help="Host-local state shared by workers, not a Syncthing directory.",
    )
    @click.option("--card", required=True)
    @click.option("--agent", required=True)
    @click.option("--request", type=click.Path(exists=True, path_type=Path), required=True)
    def run(home, state_dir, card, agent, request):
        """Execute a request whose exact hash and quality gate are bound to a card.

        The machine-owned card must link runtime_input_sha256 and quality_gate=PASS.
        These are existing coordination evidence links, not a human approval gate.
        """
        from skcoord.card_store import CardStore

        from ..link_review_work import card_generation
        from ..skrsi_handoffs import HandoffRuntime
        from ..skrsi_runtime import SKRSIRuntime

        if request.stat().st_size > 1_000_000:
            raise click.ClickException("request exceeds 1 MB")
        encoded = request.read_bytes()
        if len(encoded) > 1_000_000:
            raise click.ClickException("request exceeds 1 MB")
        input_hash = hashlib.sha256(encoded).hexdigest()
        store = CardStore(home)

        def current():
            return store.fold(card)

        def authorized():
            c = current()
            return bool(
                c
                and c.owner == agent
                and not c.archived
                and c.status.value in {"doing", "ready"}
                and "sklegal" not in (c.title + " " + " ".join(c.labels)).lower()
                and all(
                    (d := store.fold(dep)) and d.status.value == "done" for dep in c.dependencies
                )
            )

        def quality():
            c = current()
            return bool(
                c
                and c.links.get("runtime_input_sha256") == input_hash
                and c.links.get("quality_gate") == "PASS"
            )

        def authority():
            c = current()
            return card_generation(c) if c else ""

        if not authorized() or not quality():
            raise click.ClickException(
                "card authorization or machine quality evidence unavailable"
            )
        revision = authority()
        runtime = SKRSIRuntime(
            HandoffRuntime(state_dir / "handoffs.sqlite3"),
            authorize=authorized,
            quality=quality,
            authority=authority,
        )
        try:
            payload = json.loads(encoded)
            if (
                payload.get("experiment_id", card) != card
                or payload.get("item", {}).get("source_card", card) != card
            ):
                raise ValueError("request cannot mutate or project another card")
            if (
                payload.get("operation") == "review"
                and payload["item"].get("source_owner") != agent
            ):
                raise ValueError("review producer must match claimed source owner")
            result = dispatch(runtime, store, payload, agent, revision, home)
        except (ValueError, KeyError, TypeError) as exc:
            for receipt in runtime.handoffs.notifications():
                click.echo(json.dumps({"notification": receipt}, sort_keys=True), err=True)
            raise click.ClickException(
                type(exc).__name__ + ": request rejected; inspect handoff receipts"
            ) from exc
        click.echo(json.dumps(result, sort_keys=True))


def dispatch(runtime, store, request, agent, revision, home):
    """Route known metadata operations through the contract-enforcing facade."""
    from ..skrsi_evaluator import Cohort, EvaluationPolicy, Guardrail
    from ..skrsi_registry import TargetRevision

    operation = request["operation"]
    if operation == "register":
        return runtime.register(TargetRevision.from_dict(request["target"]), revision=revision)
    if operation == "collect":
        return runtime.collect(
            request["source"],
            request["events"],
            TargetRevision.from_dict(request["target"]),
            revision=revision,
        )
    if operation == "evaluate":
        return runtime.evaluate(
            Cohort(**request["baseline"]),
            Cohort(**request["treatment"]),
            EvaluationPolicy(**request["policy"]),
            tuple(Guardrail(**g) for g in request["guardrails"]),
            evaluator=agent,
            independent_reviewer=request["reviewer"],
            revision=revision,
        )
    if operation == "transition":
        return runtime.transition(
            store,
            request["experiment_id"],
            request["state"],
            agent=agent,
            revision=request["revision"],
            transition_id=request["transition_id"],
            evaluation=request["evaluation"],
            authority_revision=revision,
        )
    if operation == "project":
        return runtime.project(
            store,
            request["experiment_id"],
            revision=revision,
            expected_experiment_revision=request["experiment_revision"],
        )
    if operation == "deliver":
        return runtime.deliver(
            request["query_id"],
            request["projection_hash"],
            lambda: request["projection"],
            revision=revision,
        )
    if operation == "canary-review":
        return runtime.canary_review(request["evaluation"], revision=revision)
    if operation == "review":
        return runtime.review(home, request["item"], request["evidence_sha256"], revision=revision)
    raise ValueError("unknown metadata operation")
