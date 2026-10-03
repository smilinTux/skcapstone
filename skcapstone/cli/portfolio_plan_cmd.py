"""Read-only ``coord portfolio-plan`` command registration."""

from __future__ import annotations

from typing import TextIO

import click
from pydantic import ValidationError


def register_portfolio_plan_command(coord: click.Group) -> None:
    """Register the shadow-only portfolio evaluator."""

    @coord.command("portfolio-plan")
    @click.option("--shadow", is_flag=True, required=True, help="Run without actions or writes.")
    @click.option("--format", "fmt", type=click.Choice(["json"]), default="json")
    @click.option(
        "--input",
        "input_stream",
        type=click.File("r", encoding="utf-8"),
        default="-",
        help="Frozen portfolio-plan-input.v1 JSON file (default: stdin).",
    )
    @click.option(
        "--strict",
        is_flag=True,
        help="Exit 2 when the typed result abstains.",
    )
    def portfolio_plan(shadow: bool, fmt: str, input_stream: TextIO, strict: bool) -> None:
        """Evaluate a frozen portfolio snapshot without models or mutation."""
        del shadow, fmt
        from ..portfolio_plan import evaluate_input

        try:
            result = evaluate_input(input_stream)
        except (ValidationError, ValueError) as exc:
            raise click.UsageError(f"invalid portfolio plan input: {exc}") from None
        click.echo(result.model_dump_json(indent=2))
        if strict and result.status == "abstained":
            raise click.exceptions.Exit(2)
