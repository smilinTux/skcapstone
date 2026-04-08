"""Joule economy commands: balance, history, P&L, minting, and network stats."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from ._common import AGENT_HOME, SHARED_ROOT, console


# ---------------------------------------------------------------------------
# Level thresholds for leaderboard / dashboard
# ---------------------------------------------------------------------------

_LEVEL_THRESHOLDS: list[tuple[int, str]] = [
    (50001, "Legend"),
    (15001, "Grandmaster"),
    (5001, "Master"),
    (2001, "Expert"),
    (501, "Practitioner"),
    (101, "Apprentice"),
    (0, "Rookie"),
]

_LEVEL_RANGES: list[tuple[int, int, str]] = [
    (0, 100, "Rookie"),
    (101, 500, "Apprentice"),
    (501, 2000, "Practitioner"),
    (2001, 5000, "Expert"),
    (5001, 15000, "Master"),
    (15001, 50000, "Grandmaster"),
    (50001, 999_999_999, "Legend"),
]


def _get_level(balance: int) -> str:
    """Return the level name for a given Joule balance."""
    for threshold, name in _LEVEL_THRESHOLDS:
        if balance >= threshold:
            return name
    return "Rookie"


def _get_level_progress(balance: int) -> tuple[str, int, int, int]:
    """Return (level_name, current_in_band, band_size, next_threshold).

    For the progress bar: how far through the current level band.
    """
    for low, high, name in _LEVEL_RANGES:
        if low <= balance <= high:
            current_in_band = balance - low
            band_size = high - low + 1
            return name, current_in_band, band_size, high + 1
    # Legend has no cap
    return "Legend", balance, balance, balance


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

    # -- leaderboard ---------------------------------------------------------

    @joule_group.command("leaderboard")
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    def leaderboard_cmd(json_out: bool):
        """Rank all agents by total Joule balance."""
        from ..skjoule import JouleEngine, WalletSnapshot

        engine = JouleEngine(home=Path(SHARED_ROOT).expanduser())
        agents_dir = Path(SHARED_ROOT).expanduser() / "agents"

        entries: list[dict] = []
        if agents_dir.exists():
            for agent_dir in sorted(agents_dir.iterdir()):
                if not agent_dir.is_dir():
                    continue
                wallet_file = agent_dir / "wallet" / "joules.json"
                txn_file = agent_dir / "wallet" / "transactions.jsonl"
                if not wallet_file.exists():
                    continue
                try:
                    data = json.loads(wallet_file.read_text(encoding="utf-8"))
                    snap = WalletSnapshot(**data)

                    # Count tasks completed (mint transactions)
                    tasks_completed = 0
                    if txn_file.exists():
                        for line in txn_file.read_text(encoding="utf-8").strip().splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                txn = json.loads(line)
                                if txn.get("kind") == "mint":
                                    tasks_completed += 1
                            except (json.JSONDecodeError, ValueError):
                                continue

                    entries.append({
                        "agent": snap.agent,
                        "balance": snap.balance,
                        "tasks_completed": tasks_completed,
                        "level": _get_level(snap.balance),
                    })
                except (json.JSONDecodeError, OSError, ValueError):
                    continue

        # Sort by balance descending
        entries.sort(key=lambda e: e["balance"], reverse=True)

        # Assign ranks
        for i, entry in enumerate(entries, 1):
            entry["rank"] = i

        if json_out:
            click.echo(json.dumps(entries, indent=2))
            return

        from rich.table import Table

        if not entries:
            console.print("\n[dim]No agent wallets found.[/]\n")
            return

        table = Table(
            title="Joule Leaderboard",
            box=None, padding=(0, 2),
        )
        table.add_column("Rank", justify="center", width=6)
        table.add_column("Agent", style="bold", width=20)
        table.add_column("Balance (Joules)", justify="right", width=18)
        table.add_column("Tasks Completed", justify="right", width=16)
        table.add_column("Level", width=14)

        _RANK_COLORS = {1: "bold gold1", 2: "bold grey78", 3: "bold dark_orange3"}
        _LEVEL_COLORS = {
            "Legend": "bold bright_magenta",
            "Grandmaster": "bold red",
            "Master": "bold yellow",
            "Expert": "bold cyan",
            "Practitioner": "bold green",
            "Apprentice": "bold blue",
            "Rookie": "dim",
        }

        for entry in entries:
            rank = entry["rank"]
            rank_style = _RANK_COLORS.get(rank, "")
            rank_str = f"[{rank_style}]{rank}[/]" if rank_style else str(rank)

            level = entry["level"]
            level_style = _LEVEL_COLORS.get(level, "")
            level_str = f"[{level_style}]{level}[/]" if level_style else level

            bal_color = "green" if entry["balance"] > 0 else "dim"

            table.add_row(
                rank_str,
                entry["agent"],
                f"[{bal_color}]{entry['balance']:,}J[/]",
                str(entry["tasks_completed"]),
                level_str,
            )

        console.print()
        console.print(table)
        console.print()

    # -- dashboard -----------------------------------------------------------

    @joule_group.command("dashboard")
    @click.option(
        "--agent", "-a", "agent_name", default=None,
        help="Agent name (default: current agent).",
    )
    def dashboard_cmd(agent_name: str | None):
        """Show a financial dashboard for an agent."""
        from rich.columns import Columns
        from rich.panel import Panel
        from rich.progress_bar import ProgressBar
        from rich.table import Table
        from rich.text import Text

        from ..skjoule import JouleEngine, TransactionKind

        from .. import active_agent_name

        agent_name = agent_name or active_agent_name()
        engine = JouleEngine(home=Path(SHARED_ROOT).expanduser())
        wallet = engine.get_wallet(agent_name)
        balance = wallet.balance
        txns = wallet.get_transactions(limit=9999)

        # ---- Current Balance panel ----
        bal_color = "green" if balance > 0 else "yellow"
        balance_panel = Panel(
            f"[{bal_color} bold]{balance:,}J[/]",
            title="[cyan]Current Balance[/]",
            border_style="cyan",
            padding=(1, 4),
        )

        # ---- Income panel (7d / 30d / all time) ----
        now = datetime.now(timezone.utc)
        cutoff_7d = now - timedelta(days=7)
        cutoff_30d = now - timedelta(days=30)
        income_7d = 0
        income_30d = 0
        income_all = 0

        for txn in txns:
            if txn.kind not in (TransactionKind.MINT, TransactionKind.TRANSFER_IN):
                continue
            income_all += txn.amount
            try:
                ts = datetime.fromisoformat(txn.timestamp)
                if ts >= cutoff_30d:
                    income_30d += txn.amount
                if ts >= cutoff_7d:
                    income_7d += txn.amount
            except (ValueError, TypeError):
                pass

        income_lines = [
            f"[bold]Last 7 days:[/]   [green]{income_7d:,}J[/]",
            f"[bold]Last 30 days:[/]  [green]{income_30d:,}J[/]",
            f"[bold]All time:[/]      [green]{income_all:,}J[/]",
        ]
        income_panel = Panel(
            "\n".join(income_lines),
            title="[cyan]Income[/]",
            border_style="cyan",
            padding=(1, 2),
        )

        # ---- Top earning categories ----
        category_totals: dict[str, int] = {}
        for txn in txns:
            if txn.kind != TransactionKind.MINT:
                continue
            # Infer category from description keywords
            desc_lower = (txn.description or "").lower()
            cat = "other"
            if any(w in desc_lower for w in ("code", "commit", "bug", "test", "review", "dev")):
                cat = "development"
            elif any(w in desc_lower for w in ("sale", "consult", "business", "revenue")):
                cat = "business"
            elif any(w in desc_lower for w in ("community", "outreach", "docs")):
                cat = "community"
            elif any(w in desc_lower for w in ("deploy", "task", "ops", "incident", "operation")):
                cat = "operations"
            elif any(w in desc_lower for w in ("physical", "hardware", "infra")):
                cat = "physical"
            category_totals[cat] = category_totals.get(cat, 0) + txn.amount

        cat_table = Table(box=None, padding=(0, 2), show_header=True)
        cat_table.add_column("Category", style="bold", width=14)
        cat_table.add_column("Earned", justify="right", width=12)

        for cat, total in sorted(category_totals.items(), key=lambda x: -x[1]):
            cat_table.add_row(cat.capitalize(), f"[green]{total:,}J[/]")

        if not category_totals:
            cat_table.add_row("[dim]No data[/]", "")

        cat_panel = Panel(
            cat_table,
            title="[cyan]Top Earning Categories[/]",
            border_style="cyan",
        )

        # ---- Recent transactions (last 10) ----
        recent_txns = txns[:10]
        txn_table = Table(box=None, padding=(0, 1), show_header=True)
        txn_table.add_column("Time", style="dim", width=16)
        txn_table.add_column("Kind", width=12)
        txn_table.add_column("Amount", justify="right", width=10)
        txn_table.add_column("Description", max_width=36)

        _KIND_STYLE = {
            "mint": ("bold green", "+"),
            "spend": ("bold red", "-"),
            "transfer_in": ("bold cyan", "+"),
            "transfer_out": ("bold yellow", "-"),
        }

        for txn in recent_txns:
            style, sign = _KIND_STYLE.get(txn.kind.value, ("", ""))
            amount_str = f"[{style}]{sign}{txn.amount:,}J[/]" if style else f"{txn.amount:,}J"
            ts_short = txn.timestamp[:16].replace("T", " ") if txn.timestamp else ""
            desc = (txn.description or "")[:36]
            txn_table.add_row(ts_short, txn.kind.value.replace("_", " "), amount_str, desc)

        if not recent_txns:
            txn_table.add_row("[dim]No transactions[/]", "", "", "")

        txn_panel = Panel(
            txn_table,
            title="[cyan]Recent Transactions[/]",
            border_style="cyan",
        )

        # ---- Level + progress bar ----
        level, current_in_band, band_size, next_threshold = _get_level_progress(balance)
        pct = min(100, int((current_in_band / max(band_size, 1)) * 100))

        level_style = {
            "Legend": "bold bright_magenta",
            "Grandmaster": "bold red",
            "Master": "bold yellow",
            "Expert": "bold cyan",
            "Practitioner": "bold green",
            "Apprentice": "bold blue",
            "Rookie": "dim",
        }.get(level, "")

        if level == "Legend":
            progress_line = "[bright_magenta]MAX LEVEL[/]"
        else:
            filled = pct // 5
            empty = 20 - filled
            bar = f"[green]{'█' * filled}[/][dim]{'░' * empty}[/]"
            progress_line = f"{bar}  {pct}%  ({balance:,} / {next_threshold - 1:,}J)"

        level_lines = [
            f"[bold]Level:[/]  [{level_style}]{level}[/]",
            "",
            f"[bold]Progress to next level:[/]",
            progress_line,
        ]
        level_panel = Panel(
            "\n".join(level_lines),
            title="[cyan]Agent Level[/]",
            border_style="cyan",
            padding=(1, 2),
        )

        # ---- Render everything ----
        console.print()
        console.print(
            f"[bold cyan]  Joule Dashboard — {agent_name}[/]",
        )
        console.print()
        console.print(Columns([balance_panel, income_panel], equal=True, padding=(0, 2)))
        console.print(cat_panel)
        console.print(txn_panel)
        console.print(level_panel)
        console.print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_agent(agent_name: str | None) -> str:
    """Resolve agent name from argument, env, or package default."""
    if agent_name:
        return agent_name
    from .. import SKCAPSTONE_AGENT
    from .. import active_agent_name

    return SKCAPSTONE_AGENT or active_agent_name() or ""
