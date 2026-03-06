"""Joule economy commands: balance, history, P&L, minting, and network stats."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ._common import AGENT_HOME, SHARED_ROOT, console


def register_joule_commands(main: click.Group) -> None:
    """Register the ``skcapstone joule`` command group."""

    @main.group("joule")
    def joule_group():
        """SKJoule economic engine -- Joule balance, history, and minting.

        Joules are the unit of useful work in the SKWorld economy.
        They are earned through verified contributions and tracked
        with cryptographic proof.
        """

    # -- balance -------------------------------------------------------------

    @joule_group.command("balance")
    @click.option(
        "--agent", "agent_name", default=None,
        help="Agent name (default: current agent).",
    )
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    def balance_cmd(agent_name: str | None, json_out: bool):
        """Show the Joule wallet balance for an agent."""
        from ..skjoule import JouleEngine

        agent_name = _resolve_agent(agent_name)
        engine = JouleEngine(home=Path(SHARED_ROOT).expanduser())
        wallet = engine.get_wallet(agent_name)

        if json_out:
            click.echo(json.dumps({
                "agent": agent_name,
                "balance": wallet.balance,
                "total_minted": wallet.total_minted,
                "total_spent": wallet.total_spent,
            }, indent=2))
            return

        from rich.panel import Panel

        balance_color = "green" if wallet.balance > 0 else "yellow"
        lines = [
            f"[bold]Agent:[/]         {agent_name}",
            f"[bold]Balance:[/]       [{balance_color}]{wallet.balance:,}J[/]",
            f"[bold]Total minted:[/]  {wallet.total_minted:,}J",
            f"[bold]Total spent:[/]   {wallet.total_spent:,}J",
        ]
        console.print()
        console.print(Panel(
            "\n".join(lines),
            title="[cyan]Joule Wallet[/]",
            border_style="cyan",
        ))
        console.print()

    # -- history -------------------------------------------------------------

    @joule_group.command("history")
    @click.option(
        "--agent", "agent_name", default=None,
        help="Agent name (default: current agent).",
    )
    @click.option("--limit", "-n", default=20, help="Number of transactions to show.")
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    def history_cmd(agent_name: str | None, limit: int, json_out: bool):
        """Show recent Joule transaction history."""
        from ..skjoule import JouleEngine

        agent_name = _resolve_agent(agent_name)
        engine = JouleEngine(home=Path(SHARED_ROOT).expanduser())
        wallet = engine.get_wallet(agent_name)
        txns = wallet.get_transactions(limit=limit)

        if json_out:
            click.echo(json.dumps(
                [t.model_dump() for t in txns], indent=2
            ))
            return

        from rich.table import Table

        if not txns:
            console.print(f"\n[dim]No transactions found for {agent_name}.[/]\n")
            return

        table = Table(
            title=f"Joule Transactions -- {agent_name} (last {limit})",
            box=None, padding=(0, 2),
        )
        table.add_column("Time", style="dim", width=20)
        table.add_column("Kind", width=14)
        table.add_column("Amount", justify="right", width=10)
        table.add_column("Balance", justify="right", width=10)
        table.add_column("Description", max_width=50)

        _KIND_STYLE = {
            "mint": "[bold green]+",
            "spend": "[bold red]-",
            "transfer_in": "[bold cyan]+",
            "transfer_out": "[bold yellow]-",
        }

        for txn in txns:
            prefix = _KIND_STYLE.get(txn.kind.value, "")
            sign = "+" if txn.kind.value in ("mint", "transfer_in") else "-"
            amount_str = f"{prefix}{sign}{txn.amount:,}J[/]" if prefix else f"{sign}{txn.amount:,}J"
            # Truncate timestamp for display
            ts_short = txn.timestamp[:19].replace("T", " ") if txn.timestamp else ""
            desc = txn.description[:50] if txn.description else ""
            if txn.counterparty and txn.kind.value in ("transfer_in", "transfer_out"):
                desc = f"({txn.counterparty}) {desc}"
            table.add_row(
                ts_short,
                txn.kind.value.replace("_", " "),
                amount_str,
                f"{txn.balance_after:,}J",
                desc,
            )

        console.print()
        console.print(table)
        console.print()

    # -- pl ------------------------------------------------------------------

    @joule_group.command("pl")
    @click.option(
        "--agent", "agent_name", default=None,
        help="Agent name (default: current agent).",
    )
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    def pl_cmd(agent_name: str | None, json_out: bool):
        """Show the profit-and-loss statement for an agent."""
        from ..skjoule import JouleEngine

        agent_name = _resolve_agent(agent_name)
        engine = JouleEngine(home=Path(SHARED_ROOT).expanduser())
        pl = engine.get_agent_pl(agent_name)

        if json_out:
            click.echo(json.dumps(pl.model_dump(), indent=2))
            return

        from rich.panel import Panel

        net_color = "green" if pl.net_joules >= 0 else "red"
        cost_color = "green" if pl.llm_cost_usd < 0.01 else "yellow"

        lines = [
            f"[bold]Agent:[/]              {pl.agent}",
            f"[bold]Period:[/]             {pl.period}",
            "",
            "[bold underline]Revenue[/]",
            f"  Joules earned:       [green]{pl.joules_earned:,}J[/]",
            f"  Transfers in:        [cyan]{pl.joules_transferred_in:,}J[/]",
            "",
            "[bold underline]Costs[/]",
            f"  Joules spent:        [red]{pl.joules_spent:,}J[/]",
            f"  Transfers out:       [yellow]{pl.joules_transferred_out:,}J[/]",
            f"  LLM API costs:       [{cost_color}]${pl.llm_cost_usd:.4f} USD[/]",
            "",
            "[bold underline]Summary[/]",
            f"  Net Joules:          [{net_color}]{pl.net_joules:,}J[/]",
            f"  Current balance:     {pl.current_balance:,}J",
        ]

        console.print()
        console.print(Panel(
            "\n".join(lines),
            title="[cyan]Joule P&L Statement[/]",
            border_style="cyan",
        ))
        console.print()

    # -- mint ----------------------------------------------------------------

    @joule_group.command("mint")
    @click.option(
        "--worker", required=True,
        help="Agent or person who performed the work.",
    )
    @click.option(
        "--category", required=True,
        type=click.Choice(
            ["development", "business", "community", "operations", "physical"],
            case_sensitive=False,
        ),
        help="Work category.",
    )
    @click.option("--description", required=True, help="Description of the work.")
    @click.option("--joules", required=True, type=int, help="Number of Joules to mint.")
    @click.option("--proof", default="", help="Proof hash (auto-generated if empty).")
    def mint_cmd(worker: str, category: str, description: str, joules: int, proof: str):
        """Manually mint Joules for a verified work contribution."""
        from ..skjoule import JouleEngine, WorkCategory

        if joules <= 0:
            console.print("[red]Error: Joules must be a positive integer.[/]")
            raise SystemExit(1)

        engine = JouleEngine(home=Path(SHARED_ROOT).expanduser())
        record = engine.record_work(
            worker=worker,
            category=category,
            description=description,
            proof_hash=proof,
            joules=joules,
        )

        wallet = engine.get_wallet(worker)
        console.print()
        console.print(
            f"[bold green]Minted {record.joules:,}J[/] for "
            f"[bold]{worker}[/] ({record.category.value})"
        )
        console.print(f"  Description: {record.description}")
        console.print(f"  Proof:       {record.proof_hash[:16]}...")
        console.print(f"  New balance: [cyan]{wallet.balance:,}J[/]")
        console.print()

    # -- network -------------------------------------------------------------

    @joule_group.command("network")
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    def network_cmd(json_out: bool):
        """Show network-wide Joule economy statistics."""
        from ..skjoule import JouleEngine

        engine = JouleEngine(home=Path(SHARED_ROOT).expanduser())
        stats = engine.get_network_stats()

        if json_out:
            click.echo(json.dumps(stats.model_dump(), indent=2))
            return

        from rich.panel import Panel
        from rich.table import Table

        lines = [
            f"[bold]Total minted:[/]    [green]{stats.total_minted:,}J[/]",
            f"[bold]Total spent:[/]     [red]{stats.total_spent:,}J[/]",
            f"[bold]Total transfers:[/] {stats.total_transfers:,}J",
            f"[bold]Active agents:[/]   {stats.active_agents}",
        ]

        console.print()
        console.print(Panel(
            "\n".join(lines),
            title="[cyan]SKJoule Network[/]",
            border_style="cyan",
        ))

        if stats.agent_balances:
            table = Table(
                title="Agent Balances", box=None, padding=(0, 2),
            )
            table.add_column("Agent", style="bold")
            table.add_column("Balance", justify="right")

            for agent, balance in sorted(
                stats.agent_balances.items(), key=lambda x: -x[1]
            ):
                bal_color = "green" if balance > 0 else "dim"
                table.add_row(agent, f"[{bal_color}]{balance:,}J[/]")

            console.print(table)

        console.print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_agent(agent_name: str | None) -> str:
    """Resolve agent name from argument, env, or package default."""
    if agent_name:
        return agent_name
    from .. import SKCAPSTONE_AGENT
    return SKCAPSTONE_AGENT or "lumina"
