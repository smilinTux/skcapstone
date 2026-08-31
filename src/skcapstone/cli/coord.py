"""Coordination board commands: status, create, claim, complete, board, changelog, briefing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ._common import AGENT_HOME, console, logger
from ._validators import validate_agent_name, validate_task_id


def _status_summary(views, malformed, eligibility) -> dict:
    """Build the summary block for the bounded JSON status payload."""
    return {
        "total": len(views),
        "open": sum(1 for v in views if v.status.value == "open"),
        "claimed": sum(1 for v in views if v.status.value == "claimed"),
        "in_progress": sum(1 for v in views if v.status.value == "in_progress"),
        "done": sum(1 for v in views if v.status.value == "done"),
        "malformed": len(malformed),
        "leaf_eligible": eligibility.leaves,
        "review_needs_identity": eligibility.review,
    }


def _status_scope(tag, parent, status_filter):
    """Build a stable scope identifier for the status cursor.

    The scope binds the cursor to the exact filters (tags, parent, status)
    so a caller cannot replay a cursor from a different scope. The scope is
    hashed so it stays bounded even with many tags.
    """
    import hashlib
    import json as _json

    scope_obj = {
        "tag": [t.lower() for t in tag],
        "parent": parent,
        "status": status_filter,
    }
    body = _json.dumps(scope_obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()[:32]


def _encode_status_cursor(payload: dict) -> str:
    """Encode an opaque, integrity-protected status cursor.

    The cursor is base64 of (canonical JSON body + SHA-256 HMAC), matching the
    CardStore cursor contract: opaque to the caller, verifiable, bound to the
    scope, limit, and position.
    """
    import base64
    import hashlib
    import json as _json

    body = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hashlib.sha256(body).digest()
    cursor = base64.urlsafe_b64encode(body + signature).decode().rstrip("=")
    if len(cursor) > 4096:
        raise click.ClickException("status cursor exceeds its encoded-size contract")
    return cursor


def _decode_status_cursor(cursor: str, limit: int | None = None) -> str:
    """Decode + validate a status cursor, returning the 'after' position.

    Fails closed on a malformed, tampered, oversized, or stale cursor (one
    minted for a different limit/scope).
    """
    import base64
    import binascii
    import hashlib
    import hmac
    import json as _json

    if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
        raise click.ClickException("status cursor is malformed")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        body, signature = raw[:-32], raw[-32:]
        expected = hashlib.sha256(body).digest()
        if len(signature) != 32 or not hmac.compare_digest(signature, expected):
            raise ValueError("signature mismatch")
        payload = _json.loads(body)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"after", "limit", "scope", "v"}
            or payload["v"] != 1
            or not isinstance(payload.get("after"), str)
            or not payload["after"]
            or (limit is not None and payload.get("limit") != limit)
        ):
            raise ValueError("cursor is stale or out of scope")
        return payload["after"]
    except (
        ValueError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        binascii.Error,
    ) as exc:
        raise click.ClickException("status cursor is malformed or stale") from exc


def register_coord_commands(main: click.Group) -> None:
    """Register the coord command group."""

    @main.group()
    def coord():
        """Multi-agent coordination board.

        Create tasks, claim work, and track progress across
        agents. All data lives in ~/.skcapstone/coordination/
        and syncs via Syncthing. Conflict-free by design.
        """

    from .coord_amend import register_coord_amend_commands
    from .portfolio_plan_cmd import register_portfolio_plan_command

    register_coord_amend_commands(coord)
    register_portfolio_plan_command(coord)

    @coord.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--tag", multiple=True, help="Only tasks carrying this tag (repeatable).")
    @click.option(
        "--parent",
        default=None,
        help="Only tasks tagged 'parent-<id>' (children of this epic/card).",
    )
    @click.option(
        "--status",
        "status_filter",
        default=None,
        type=click.Choice(["open", "claimed", "in_progress", "review", "done", "blocked"]),
        help="Only tasks in this status.",
    )
    @click.option(
        "--limit",
        default=None,
        type=int,
        help="Bound the status payload to at most this many cards.",
    )
    @click.option(
        "--cursor",
        default=None,
        help="Opaque continuation cursor from a previous bounded status call.",
    )
    @click.option(
        "--format",
        "fmt",
        default="text",
        type=click.Choice(["text", "json"]),
        help="text = Rich human-readable board (default); json = machine-readable payload.",
    )
    def coord_status(home, tag, parent, status_filter, limit, cursor, fmt):
        """Show the coordination board overview.

        Bounded, machine-readable contract (SKCOORD-STATUS-BOUND-01):
        The primary payload goes to stdout. With --format json the stdout is
        a single JSON document: the bounded set of task rows, a summary, the
        malformed-card report (ID + evidence hash), and a discoverable
        continuation cursor when the scope is truncated. Diagnostics (skipped
        files, unreadable cards) are emitted on stderr, keeping stdout clean
        and parseable by machines.
        """
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        views = board.get_task_views()
        agents = board.load_agents()
        # Bounded interface: report one unreadable card by ID + evidence hash
        # without crashing or hiding the readable cards. Degrades to an empty
        # malformed list when the paired skcoord build predates the function.
        try:
            from ..card_store import task_views_with_malformed

            _, malformed = task_views_with_malformed(home_path)
        except ImportError:
            malformed: list = []
        for entry in malformed:
            logger.warning(
                "Malformed card %s (source %s): %s [evidence_sha256=%s]",
                entry["card_id"],
                entry["source"],
                entry["reason"],
                entry["evidence_sha256"],
            )

        # Open cards whose dependencies are not all done are blocked: the
        # claim gate refuses them without --force, so status must say so.
        done_ids = {v.task.id for v in views if v.status.value == "done"}

        def _status_label(view) -> str:
            if (
                view.status.value == "open"
                and view.task.dependencies
                and not set(view.task.dependencies).issubset(done_ids)
            ):
                return "blocked"
            return view.status.value

        if parent:
            tag = (*tag, f"parent-{parent}")
        if tag:
            wanted = {t.lower() for t in tag}
            views = [v for v in views if wanted & {t.lower() for t in v.task.tags}]
        if status_filter:
            views = [v for v in views if _status_label(v) == status_filter]

        # Eligibility counts are needed for both the text and JSON payloads.
        # The eligibility read is best-effort: a malformed card in the
        # CardStore event stream must not crash the whole status command.
        from ..coord_eligibility import leaf_eligibility_counts

        try:
            eligibility = leaf_eligibility_counts(home_path, {v.task.id for v in views})
        except (ValueError, OSError) as exc:
            logger.warning(
                "eligibility counts unavailable, treating malformed population as 1: %s", exc
            )
            from dataclasses import dataclass as _dc

            @ _dc(frozen=True)
            class _NoCounts:
                leaves: int = 0
                review: int = 0
                malformed: int = 1

            eligibility = _NoCounts()

        # Bounded + machine-readable payload. When --limit is given (or the
        # caller passes --cursor), the primary payload is bounded to at most
        # ``limit`` cards, and truncation is exposed via a discoverable
        # continuation cursor. With --format json the whole payload goes to
        # stdout as one JSON document; diagnostics already went to stderr.
        bounded = limit is not None or cursor is not None
        if limit is not None and limit <= 0:
            raise click.ClickException("--limit must be >= 1")

        def _row(view):  # noqa: E306 - local helper for row projection
            return {
                "id": view.task.id,
                "title": view.task.title,
                "priority": view.task.priority.value,
                "status": _status_label(view),
                "claimed_by": view.claimed_by,
                "tags": view.task.tags,
            }

        if fmt == "json" or bounded:
            if cursor is not None:
                after = _decode_status_cursor(cursor, limit=limit)
                views_from = [v for v in views if v.task.id > after]
                page = views_from[:limit] if limit is not None else views_from
                has_more = limit is not None and len(views_from) > limit
                next_cursor = None
                if has_more and page:
                    next_cursor = _encode_status_cursor(
                        {
                            "after": page[-1].task.id,
                            "limit": limit,
                            "scope": _status_scope(tag, parent, status_filter),
                            "v": 1,
                        },
                    )
                payload = {
                    "format": "json",
                    "summary": _status_summary(views, malformed, eligibility),
                    "cards": [_row(v) for v in page],
                    "malformed_cards": malformed,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                }
                click.echo(json.dumps(payload, indent=2, default=str))
                return

            page = views[:limit] if limit is not None else views
            has_more = limit is not None and len(views) > limit
            next_cursor = None
            if has_more and page:
                next_cursor = _encode_status_cursor(
                    {
                        "after": page[-1].task.id,
                        "limit": limit,
                        "scope": _status_scope(tag, parent, status_filter),
                        "v": 1,
                    },
                )
            payload = {
                "format": "json",
                "summary": _status_summary(views, malformed, eligibility),
                "cards": [_row(v) for v in page],
                "malformed_cards": malformed,
                "has_more": has_more,
                "next_cursor": next_cursor,
            }
            click.echo(json.dumps(payload, indent=2, default=str))
            return

        if not views and (tag or status_filter):
            console.print("\n  [dim]No tasks match the given filters.[/]\n")
            return

        if not views and not agents:
            console.print("\n  [dim]Board is empty. Create tasks with:[/]")
            console.print("  [cyan]skcapstone coord create --title 'My Task'[/]\n")
            return

        open_count = sum(1 for v in views if v.status.value == "open")
        progress_count = sum(1 for v in views if v.status.value == "in_progress")
        claimed_count = sum(1 for v in views if v.status.value == "claimed")
        done_count = sum(1 for v in views if v.status.value == "done")
        from ..coord_eligibility import leaf_eligibility_counts

        eligibility = leaf_eligibility_counts(home_path, {v.task.id for v in views})

        console.print()
        console.print(
            Panel(
                f"[bold]Tasks:[/] {len(views)} total  "
                f"[green]{open_count} open[/]  "
                f"[bold green]{eligibility.leaves} leaf eligible[/]  "
                f"[magenta]{eligibility.review} review needs identity[/]  "
                f"[red]{eligibility.malformed} malformed[/]  "
                f"[cyan]{claimed_count} claimed[/]  "
                f"[yellow]{progress_count} in progress[/]  "
                f"[dim]{done_count} done[/]",
                title="Coordination Board",
                border_style="bright_blue",
            )
        )

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style="cyan", max_width=10)
        table.add_column("Title", style="bold")
        table.add_column("Priority")
        table.add_column("Status")
        table.add_column("Assignee", style="dim")
        table.add_column("Tags", style="dim")

        priority_colors = {"critical": "bold red", "high": "red", "medium": "yellow", "low": "dim"}
        status_colors = {
            "open": "green",
            "claimed": "cyan",
            "in_progress": "yellow",
            "done": "dim",
            "blocked": "red",
        }

        for v in views:
            if v.status.value == "done" and status_filter is None:
                continue
            t = v.task
            status_label = _status_label(v)
            p_style = priority_colors.get(t.priority.value, "dim")
            s_style = status_colors.get(status_label, "dim")
            table.add_row(
                t.id,
                t.title,
                Text(t.priority.value.upper(), style=p_style),
                Text(status_label.upper(), style=s_style),
                v.claimed_by or "",
                ", ".join(t.tags),
            )

        console.print(table)

        if agents:
            console.print()
            for ag in agents:
                icon = {"active": "[green]ACTIVE[/]", "idle": "[yellow]IDLE[/]"}.get(
                    ag.state.value, "[dim]OFFLINE[/]"
                )
                current = f" -> [cyan]{ag.current_task}[/]" if ag.current_task else ""
                console.print(f"  {icon} [bold]{ag.agent}[/]{current}")
        console.print()

    @coord.command("create")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--title", required=True, help="Task title.")
    @click.option(
        "--desc",
        default="",
        help="Task description. Reference repo-relative paths (e.g. src/foo.py), "
        "never absolute ones - see 'coord rehome'.",
    )
    @click.option(
        "--priority", type=click.Choice(["critical", "high", "medium", "low"]), default="medium"
    )
    @click.option("--tag", multiple=True, help="Tags (repeatable).")
    @click.option("--by", default="human", help="Creator name.")
    @click.option("--criteria", multiple=True, help="Acceptance criteria (repeatable).")
    @click.option("--dep", multiple=True, help="Dependency task IDs (repeatable).")
    def coord_create(home, title, desc, priority, tag, by, criteria, dep):
        """Create a new task on the board."""
        from ..coordination import Board, Task, TaskPriority

        validate_agent_name(by)
        for d in dep:
            validate_task_id(d)

        home_path = Path(home).expanduser()
        board = Board(home_path)
        task = Task(
            title=title,
            description=desc,
            priority=TaskPriority(priority),
            tags=list(tag),
            created_by=by,
            acceptance_criteria=list(criteria),
            dependencies=list(dep),
        )
        path = board.create_task(task)
        console.print(f"\n  [green]Created:[/] [{task.id}] {task.title}")
        console.print(f"  [dim]{path}[/]\n")

    @coord.command("claim")
    @click.argument("task_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", required=True, help="Agent name claiming the task.")
    @click.option(
        "--force",
        is_flag=True,
        default=False,
        help="Compatibility flag. Dependency, review, and human gates still cannot be bypassed.",
    )
    def coord_claim(task_id, home, agent, force):
        """Claim a task for an agent.

        A task whose dependencies are not all done is blocked. The compatibility
        --force flag cannot bypass dependency, review, or human gates.
        """
        from ..coordination import Board

        validate_task_id(task_id)
        validate_agent_name(agent)

        home_path = Path(home).expanduser()
        board = Board(home_path)
        try:
            ag = board.claim_task(agent, task_id, force=force)
            console.print(f"\n  [green]Claimed:[/] [{task_id}] by [bold]{ag.agent}[/]\n")
        except ValueError as e:
            console.print(f"\n  [red]Error:[/] {e}\n")
            sys.exit(1)

    @coord.command("complete")
    @click.argument("task_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", required=True, help="Agent name completing the task.")
    def coord_complete(task_id, home, agent):
        """Mark a task as completed."""
        from ..coordination import Board

        validate_task_id(task_id)
        validate_agent_name(agent)

        home_path = Path(home).expanduser()

        # A review card must have said something before it can be closed. Without
        # this, completing one marks the parent as reviewed while leaving no record
        # of what was found, and silence reads as approval. Measured 2026-08-28:
        # 39 of 317 completed review cards had recorded no verdict at all.
        from ..review_verdict import validate_review_completion

        _title = ""
        _core = home_path / "cards" / task_id / "core.json"
        if _core.exists():
            try:
                _title = str(json.loads(_core.read_text()).get("title") or "")
            except (ValueError, OSError):
                _title = ""
        try:
            validate_review_completion(task_id, _title, home_path)
        except ValueError as e:
            console.print(f"\n  [red]Refused:[/] {e}\n")
            sys.exit(1)

        board = Board(home_path)
        try:
            ag = board.complete_task(agent, task_id)
        except ValueError as e:
            console.print(f"\n  [red]Error:[/] {e}\n")
            sys.exit(1)
        # board.complete_task() automatically mints Joules via _mint_joules_for_task
        console.print(f"\n  [green]Completed:[/] [{task_id}] by [bold]{ag.agent}[/]\n")

    @coord.command("release-claim")
    @click.argument("task_id")
    @click.option("--owner", required=True, help="Exact current claim owner.")
    @click.option("--agent", required=True, help="Audited release actor.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_release_claim(task_id, owner, agent, home):
        """Release one active claim without completing the task."""
        from ..coordination import Board

        validate_task_id(task_id)
        validate_agent_name(owner)
        validate_agent_name(agent)
        try:
            changed = Board(Path(home).expanduser()).release_claim(owner, task_id, actor=agent)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        outcome = "Released" if changed else "Already released"
        console.print(f"\n  [green]{outcome} claim on {task_id} owned by {owner}.[/]\n")

    @coord.command("score")
    @click.argument("task_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--round", "round_", required=True, type=int, help="Grading round number.")
    @click.option("--score", required=True, type=int, help="Score value (rubric 1-5).")
    @click.option("--notes", default="", help="Grader notes.")
    @click.option("--harness", default="", help="Harness / grader identity.")
    @click.option("--phase", default=None, help="Autopilot phase label.")
    @click.option("--ref", default=None, help="PR URL (http*) or artifact ref.")
    def coord_score(task_id, home, round_, score, notes, harness, phase, ref):
        """Record an autopilot grade on a task (meta.autopilot.scores)."""
        from ..coordination import Board

        validate_task_id(task_id)
        home_path = Path(home).expanduser()
        board = Board(home_path)
        try:
            path = board.score_task(
                task_id,
                round=round_,
                score=score,
                notes=notes,
                harness=harness,
                phase=phase,
                ref=ref,
            )
        except FileNotFoundError as e:
            console.print(f"\n  [red]Error:[/] {e}\n")
            sys.exit(1)
        console.print(f"\n  [green]Scored:[/] [{task_id}] round {round_} = {score}")
        console.print(f"  [dim]{path}[/]\n")

    @coord.command("board")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_board(home):
        """Generate and display the BOARD.md overview."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        path = board.write_board_md()
        md = board.generate_board_md()
        console.print(md)
        console.print(f"\n  [dim]Written to {path}[/]\n")

    @coord.command("kanban")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--html",
        "html_out",
        default=None,
        type=click.Path(),
        help="Write the visual kanban board to this HTML file.",
    )
    @click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        help="Emit the grid as JSON instead of a text summary.",
    )
    def coord_kanban(home, html_out, as_json):
        """Unified kanban board over coord tasks and ITIL tickets.

        Columns are the shared lifecycle (backlog, ready, doing, review, done);
        swimlanes are the card kind (feature, bug, security, expedite, change,
        problem). Reads both stores read-only.
        """
        import json as _json

        from ..card import COLUMN_ORDER, LANE_ORDER, KanbanBoard, render_html

        home_path = Path(home).expanduser()
        kb = KanbanBoard(home_path)

        if html_out:
            out = Path(html_out).expanduser()
            out.write_text(render_html(kb), encoding="utf-8")
            console.print(f"\n  [green]Kanban board written to {out}[/]\n")
            return

        grid = kb.grid()
        if as_json:
            payload = {
                lane: {col: [c.model_dump() for c in grid[lane][col]] for col in COLUMN_ORDER}
                for lane in LANE_ORDER
            }
            click.echo(_json.dumps(payload, indent=2))
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Swimlane", style="bold")
        for col in COLUMN_ORDER:
            table.add_column(col.capitalize(), justify="right")
        for lane in LANE_ORDER:
            counts = [len(grid[lane][col]) for col in COLUMN_ORDER]
            if not any(counts):
                continue
            table.add_row(lane, *[str(n) if n else "[dim]-[/]" for n in counts])
        console.print()
        console.print(
            Panel(table, title="Kanban (columns x swimlanes)", border_style="bright_blue")
        )
        console.print("  [dim]Full board: [cyan]coord kanban --html board.html[/][/]\n")

    @coord.command("archive-done")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--days", default=14, type=int, help="Archive done tasks older than N days.")
    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Show what would be archived without writing.",
    )
    def coord_archive_done(home, days, dry_run):
        """Age done tasks off the active board (default: older than 14 days)."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        ids = board.archive_done_tasks(older_than_days=days, dry_run=dry_run)
        verb = "Would archive" if dry_run else "Archived"
        console.print(f"\n  [green]{verb} {len(ids)} done task(s) older than {days}d.[/]\n")

    @coord.command("age-backlog")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--days", default=90, type=int, help="Archive unclaimed open tasks older than N days."
    )
    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Show what would be archived without writing.",
    )
    def coord_age_backlog(home, days, dry_run):
        """Archive ancient unclaimed open tasks (default: older than 90 days)."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        ids = board.age_stale_open(older_than_days=days, dry_run=dry_run)
        verb = "Would archive" if dry_run else "Archived"
        console.print(f"\n  [green]{verb} {len(ids)} stale open task(s) older than {days}d.[/]\n")

    @coord.command("migrate")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Report what would import without writing the CardStore.",
    )
    def coord_migrate(home, dry_run):
        """Import the legacy board (coord + ITIL + overlay) into the CardStore.

        Idempotent and additive (Phase 4). Nothing reads the CardStore until
        SKCOORD_CARD_STORE=1. Reversible: rm ~/.skcapstone/cards to undo.
        """
        from ..card_store import import_from_legacy

        home_path = Path(home).expanduser()
        res = import_from_legacy(home_path, dry_run=dry_run)
        verb = "Would import" if dry_run else "Imported"
        console.print(
            f"\n  [green]{verb} {res['imported']} card(s)"
            f" ({res['skipped']} already present, {res['total']} total).[/]\n"
        )

    @coord.command("parity")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--show", default=10, type=int, help="Max mismatches to print.")
    @click.option(
        "--check",
        is_flag=True,
        default=False,
        help="Exit non-zero on any drift (for the soak monitor).",
    )
    @click.option(
        "--open-threshold",
        default=None,
        type=int,
        help="Open-count drift beyond this raises the PARITY ALERT.",
    )
    def coord_parity(home, show, check, open_threshold):
        """Diff the legacy board against the CardStore fold (Phase 4 soak check)."""
        import sys

        from ..card_store import OPEN_DRIFT_THRESHOLD, parity_check

        home_path = Path(home).expanduser()
        threshold = OPEN_DRIFT_THRESHOLD if open_threshold is None else open_threshold
        par = parity_check(home_path, open_drift_threshold=threshold)
        ok = not par["mismatches"] and not par["missing"] and not par["open_alert"]
        color = "green" if ok else "red"
        console.print(
            f"\n  [{color}]checked={par['checked']} matched={par['matched']} "
            f"mismatches={len(par['mismatches'])} missing={len(par['missing'])}[/]"
        )
        console.print(
            f"  open: legacy={par['open_legacy']} store={par['open_store']} "
            f"drift={par['open_drift']} (threshold {par['open_drift_threshold']})"
        )
        if par["open_alert"]:
            console.print(
                f"  [bold red]PARITY ALERT: store-served open-count diverges "
                f"from legacy by {par['open_drift']} "
                f"(> {par['open_drift_threshold']}). Run 'coord migrate' then "
                f"'coord reconcile --apply' to converge.[/]"
            )
        for m in par["mismatches"][:show]:
            console.print(f"    [yellow]{m['id']}[/]: {m['diff']}")
        if par["missing"][:show]:
            console.print(f"    [yellow]missing[/]: {par['missing'][:show]}")
        # Informational diffs (priority/swimlane) are reported but never gate.
        # The dashboard writes those store-only, so legacy is the stale side by
        # design and `reconcile` deliberately will not converge them. Counting
        # them as failures made the gate unsatisfiable, which is how a gate
        # stops being read. Shown so drift stays visible, dimmed so it is
        # obviously not the thing that failed.
        info = par.get("informational") or []
        if info:
            console.print(
                f"    [dim]informational (store-authoritative, not gating): {len(info)}[/]"
            )
            for m in info[:show]:
                console.print(f"      [dim]{m['id']}: {m['diff']}[/]")
        console.print()
        if check and not ok:
            sys.exit(1)

    @coord.command("reconcile")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--apply",
        "apply_",
        is_flag=True,
        default=False,
        help="Write corrective events (default is a dry-run report).",
    )
    @click.option(
        "--allow-uncomplete",
        is_flag=True,
        default=False,
        help=(
            "Also converge cards that are done in the store but not in legacy. "
            "This MOVES THEM OUT OF DONE. Off by default."
        ),
    )
    def coord_reconcile(home, apply_, allow_uncomplete):
        """Converge the CardStore on the authoritative legacy board.

        One-time repair for drift that predates the mirror (claims/completes
        recorded only in agents/*.json): appends corrective move/assign/
        archive events, writer 'reconcile'. Append-only and idempotent.

        Never un-completes work: a card whose store state is 'done' is skipped
        and reported rather than dragged backward to match a lagging legacy
        projection. Use --allow-uncomplete to override.
        """
        from ..card_store import reconcile_from_legacy

        home_path = Path(home).expanduser()
        res = reconcile_from_legacy(
            home_path, dry_run=not apply_, allow_uncomplete=allow_uncomplete
        )
        if apply_:
            console.print(f"\n  [green]Reconciled {res['fixed']} card(s) to legacy state.[/]\n")
        else:
            console.print(
                f"\n  [yellow]Would reconcile {res['would_fix']} card(s).[/] "
                f"Re-run with --apply to write.\n"
            )
        skipped = res.get("skipped_uncomplete") or []
        if skipped:
            # Loud, not dimmed: these are the cards the gate will keep failing
            # on, and the reason is that converging them would destroy work.
            console.print(
                f"  [yellow]Skipped {len(skipped)} card(s) that are done in the "
                f"store but not in legacy.[/]\n"
                f"  Converging these would un-complete finished work, so parity "
                f"will keep reporting them.\n"
                f"  Legacy is the stale side here; fix it there, or pass "
                f"--allow-uncomplete to override.\n"
            )
            for cid in skipped[:20]:
                console.print(f"    [dim]{cid}[/]")
            if len(skipped) > 20:
                console.print(f"    [dim]... and {len(skipped) - 20} more[/]")
            console.print()

    @coord.command("export-legacy")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--apply",
        "apply_",
        is_flag=True,
        default=False,
        help="Write the legacy projection (default is a dry-run report).",
    )
    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Deprecated no-op: dry-run is now the default. Use --apply to write.",
    )
    def coord_export_legacy(home, apply_, dry_run):
        """Rebuild a current legacy board (tasks/ + agents/) from the CardStore.

        The Phase 4e-retire rollback safety net (inverse of 'coord migrate').
        After legacy writes are retired, this reconstructs a fully-current
        legacy projection from the event-sourced store. Rollback recipe:
        set SKCOORD_CARD_STORE=0, run this, restart -- legacy is authoritative
        again. Existing (immutable) task files are preserved; only the agent
        status layer is recomputed.

        DRY-RUN BY DEFAULT; pass --apply to write. This changed on 2026-08-16
        after it wrote a live board unprompted: the operator read the sibling
        'reconcile' command's --apply convention and reasonably assumed this one
        matched. It did not, and it is the more dangerous of the two.

        Two reasons this is not merely tidier. This command is the ROLLBACK
        SAFETY NET for the irreversible Phase 4e-retire step, and a safety net
        that writes by default is backwards. And "nobody runs it by accident" is
        not a safety property on this fleet, because skoperator honor mode
        actuates without a human, so the realistic trigger is automated rather
        than careless.

        The write is not destructive in the card sense (no card or status is
        lost, and it improved parity when it fired), but it DOES recompute the
        agent status layer, which collapsed per-agent completion attribution
        into a synthetic 'legacy-export' agent for entries that predate
        dual-write and therefore have no per-event owner anywhere.
        """
        from ..card_store import export_to_legacy

        home_path = Path(home).expanduser()
        res = export_to_legacy(home_path, dry_run=not apply_)
        verb = "Wrote" if apply_ else "Would write"
        colour = "green" if apply_ else "yellow"
        suffix = "" if apply_ else " Re-run with --apply to write."
        console.print(
            f"\n  [{colour}]{verb} {res['tasks_written']} new task file(s) + "
            f"{res['agents_written']} agent file(s) from {res['cards']} "
            f"store card(s).[/]{suffix}\n"
        )

    @coord.command("maintain")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--done-days", default=14, type=int, help="Archive done tasks older than N days."
    )
    @click.option(
        "--backlog-days",
        default=90,
        type=int,
        help="Archive unclaimed open tasks older than N days.",
    )
    @click.option("--dry-run", is_flag=True, default=False)
    def coord_maintain(home, done_days, backlog_days, dry_run):
        """Keep the board bounded: archive old done + ancient open tasks.

        Runs both sweeps in one shot (for the scheduler). Reversible: delete the
        per-writer archive index to restore.
        """
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        done = board.archive_done_tasks(older_than_days=done_days, dry_run=dry_run)
        stale = board.age_stale_open(older_than_days=backlog_days, dry_run=dry_run)
        verb = "Would archive" if dry_run else "Archived"
        console.print(
            f"\n  [green]{verb} {len(done)} done (>{done_days}d) + "
            f"{len(stale)} stale-open (>{backlog_days}d) = {len(done) + len(stale)} total.[/]\n"
        )

    @coord.command("move")
    @click.argument("task_id")
    @click.argument("column", type=click.Choice(["backlog", "ready", "doing", "review", "done"]))
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--order", default=None, type=int, help="Position within the column.")
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_move(task_id, column, home, order, agent):
        """Move a card to a kanban column (backlog/ready/doing/review/done)."""
        home_path = Path(home).expanduser()
        from skcoord.lifecycle import transition_task

        try:
            receipt = transition_task(
                home_path,
                task_id=task_id,
                column=column,
                actor=agent or "coord-move",
                order=order,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from None
        pos = f" at order {order}" if order is not None else ""
        console.print(f"\n  [green]Moved {task_id} to '{column}'{pos}.[/]\n")
        if receipt.actions:
            console.print(
                f"  [dim]Reconciled {len(receipt.actions)} agent projection change(s).[/]"
            )

    @coord.command("reconcile-agents")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--repair", is_flag=True, default=False)
    @click.option("--agent", default="coord-reconcile", help="Receipt writer identity.")
    @click.option("--stale-seconds", default=3600, type=click.IntRange(min=0))
    def coord_reconcile_agents(home, repair, agent, stale_seconds):
        """Audit agent projection drift and optionally repair it explicitly."""
        import json

        from skcoord.lifecycle import audit_lifecycle, repair_lifecycle

        home_path = Path(home).expanduser()
        try:
            if repair:
                receipt = repair_lifecycle(
                    home_path,
                    actor=agent,
                    stale_after_seconds=stale_seconds,
                )
                payload = receipt.to_dict()
                payload["receipt_path"] = str(receipt.receipt_path)
            else:
                payload = audit_lifecycle(home_path).to_dict()
        except (OSError, RuntimeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from None
        console.print(json.dumps(payload, indent=2))
        if not payload.get("clean", payload.get("after", {}).get("clean", False)):
            raise click.ClickException("coordination lifecycle is not reconciled")

    @coord.command("label")
    @click.argument("task_id")
    @click.argument("label")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--remove", is_flag=True, default=False, help="Remove the label instead.")
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_label(task_id, label, home, remove, agent):
        """Add (or remove) a label on a card."""
        from ..card import CardEvent, CardEventLog

        home_path = Path(home).expanduser()
        action = "remove_label" if remove else "add_label"
        CardEventLog(home_path).append(
            CardEvent(card_id=task_id, action=action, label=label, writer=agent or "")
        )
        verb = "Removed" if remove else "Added"
        console.print(f"\n  [green]{verb} label '{label}' on {task_id}.[/]\n")

    @coord.command("describe")
    @click.argument("task_id")
    @click.option("--title", default=None, help="New card title.")
    @click.option("--description", default=None, help="New card description.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_describe(task_id, title, description, home, agent):
        """Edit a card's title/description (folded, never rewrites core.json).

        Birth facts stay write-once: the edit is one appended event, so it is
        attributed to its writer and reversed by describing again. Only the
        options you pass are changed; pass an empty string to clear a field.
        """
        from ..card import CardEvent, CardEventLog

        if title is None and description is None:
            raise click.UsageError("Pass --title and/or --description.")

        home_path = Path(home).expanduser()
        try:
            CardEventLog(home_path).append(
                CardEvent(
                    card_id=task_id,
                    action="describe",
                    title=title,
                    description=description,
                    writer=agent or "",
                )
            )
            from ..card_store import card_store_write_enabled, mirror_coord_describe

            if card_store_write_enabled():
                mirror_coord_describe(
                    home_path, task_id, agent or "", title=title, description=description
                )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        changed = ", ".join(
            k for k, v in (("title", title), ("description", description)) if v is not None
        )
        console.print(f"\n  [green]Described {task_id} ({changed}).[/]\n")

    @coord.command("rehome")
    @click.argument("old_prefix")
    @click.argument("new_prefix")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to coord-rehome).")
    @click.option("--dry-run", is_flag=True, default=False, help="Report matches, write nothing.")
    def coord_rehome(old_prefix, new_prefix, home, agent, dry_run):
        """Rewrite a path prefix across every folded card description.

        For each card whose description still mentions OLD_PREFIX, appends one
        attributed describe event carrying the rewritten text - the established
        fold pattern, so core.json stays write-once and the rewrite is
        reversible by swapping the arguments. Use it after a repository move
        instead of hand-editing dozens of cards.
        """
        from ..rehome import rehome_descriptions

        home_path = Path(home).expanduser()
        try:
            report = rehome_descriptions(
                home_path, old_prefix, new_prefix, agent=agent or "", dry_run=dry_run
            )
        except ValueError as exc:
            raise click.UsageError(str(exc)) from None
        verb = "Would rewrite" if dry_run else "Rewrote"
        summary = f"{verb} {report['matched']} card(s): {old_prefix} -> {new_prefix}."
        console.print(f"\n  [green]{summary}[/]")
        for cid in report["cards"]:
            console.print(f"    [dim]- {cid}[/]")
        console.print()

    @coord.command("link")
    @click.argument("task_id")
    @click.argument("key")
    @click.argument("value")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_link(task_id, key, value, home, agent):
        """Attach a link (pr/commit/doc/...) to a card."""
        from ..blocked_verdict import validate_blocked_verdict
        from ..card import CardEvent, CardEventLog

        # A BLOCKED verdict takes a card out of circulation. It must therefore
        # say what would put it back. Measured on the live board 2026-08-27: of
        # 39 open cards whose latest outcome was BLOCKED, 18 were the literal
        # word and 20 more named no blocked_on at all, so that pool could not
        # drain. The contract was already in the worker brief; asking was not
        # enough, so it is refused here at the write path.
        try:
            validate_blocked_verdict(key, value)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None

        home_path = Path(home).expanduser()
        try:
            CardEventLog(home_path).append(
                CardEvent(
                    card_id=task_id,
                    action="link",
                    link_key=key,
                    link_value=value,
                    writer=agent or "",
                )
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        console.print(f"\n  [green]Linked {task_id}: {key} = {value}.[/]\n")

    @coord.command("changelog")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--output", "-o", default=None, type=click.Path(), help="Output file path.")
    def coord_changelog(home, output):
        """Generate CHANGELOG.md from completed board tasks."""
        from ..changelog import generate_changelog, write_changelog

        home_path = Path(home).expanduser()
        out_path = Path(output) if output else None
        path = write_changelog(home_path, out_path)

        content = generate_changelog(home_path)
        console.print(content[:3000])
        if len(content) > 3000:
            console.print(f"\n  [dim]... ({len(content)} chars total)[/]")
        console.print(f"\n  [green]Written to {path}[/]\n")

    @coord.command("briefing")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def coord_briefing(home, fmt):
        """Print the full coordination protocol for any AI agent."""
        from ..coordination import get_briefing_json, get_briefing_text

        home_path = Path(home).expanduser()
        if fmt == "json":
            click.echo(get_briefing_json(home_path))
        else:
            click.echo(get_briefing_text(home_path))
