"""Dashboard Economy view: the best single view into the cost of SKWorld's
work -- the autonomous-work cost ledger (skharness autopilot_cost) plus the
fleet-wide joule wealth (skcapstone skjoule wallets), in one JSON payload.

Both sources are lazy-imported: a dev seat without ``skharness`` or
``skcapstone`` installed still gets a well-formed, empty payload (plus an
``errors`` note) instead of a 500 -- this panel must never break the page.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_EMPTY_AGG = {"cost_usd": 0.0, "joules": 0, "tokens": 0, "runs": 0}


def _cap_usd() -> float | None:
    """Best-effort daily USD cap from the autopilot config.

    None if ``skharness.autocode.config`` is unavailable or the read fails
    for any reason -- the cap is a "nice to have" percentage-bar input, never
    something worth failing the whole Economy view over.
    """
    try:
        from skharness.autocode.config import Config

        return Config.load().caps.max_usd_per_day
    except Exception:  # noqa: BLE001 -- best-effort, never break the page
        return None


def _autopilot_section(today: str, errors: list[str]) -> dict:
    """Autopilot cost ledger: today/7d/30d/all-time + by-repo, the 30-day
    daily series, and the recent settlement journal."""
    try:
        from skharness.autocode import autopilot_cost

        return {
            "summary": autopilot_cost.summary(today=today, cap_usd=_cap_usd()),
            "cost_series": autopilot_cost.daily_series(today=today, days=30),
            "settlements": autopilot_cost.recent_settlements(limit=20),
        }
    except ImportError as exc:
        errors.append(f"autopilot_cost unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001 -- never 500 the panel
        errors.append(f"autopilot_cost failed: {exc}")
    return {
        "summary": {
            "today": dict(_EMPTY_AGG),
            "last_7_days": dict(_EMPTY_AGG),
            "last_30_days": dict(_EMPTY_AGG),
            "all_time": dict(_EMPTY_AGG),
            "by_repo": {},
            "cap_usd": None,
            "cap_joules": None,
            "today_pct_of_cap": None,
        },
        "cost_series": [],
        "settlements": [],
    }


def _joule_section(home: Path, errors: list[str]) -> dict:
    """Fleet-wide joule wealth: total circulating supply, active agent count,
    and each agent's balance + level -- reuses ``JouleEngine.get_network_stats``
    and the exact level thresholds ``skcapstone joule network``/``leaderboard``
    use (``joule_cmd._get_level``), rather than reimplementing either."""
    try:
        from skcapstone.cli.joule_cmd import _get_level
        from skcapstone.skjoule import JouleEngine

        engine = JouleEngine(home=Path(home).expanduser())
        stats = engine.get_network_stats()
        agents = sorted(
            (
                {"agent": agent, "balance": balance, "level": _get_level(balance)}
                for agent, balance in stats.agent_balances.items()
            ),
            key=lambda a: -a["balance"],
        )
        return {
            "total_supply": sum(stats.agent_balances.values()),
            "active_agents": stats.active_agents,
            "agents": agents,
        }
    except ImportError as exc:
        errors.append(f"skjoule unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001 -- never 500 the panel
        errors.append(f"skjoule failed: {exc}")
    return {"total_supply": 0, "active_agents": 0, "agents": []}


def get_economy(home: Path) -> dict:
    """Assemble the fleet-wide Economy view for ``GET /api/economy``.

    Args:
        home: Agent home directory (the same root every other dashboard
            panel queries -- board, memory, doctor, CMDB, etc).

    Returns:
        dict with ``autopilot_cost``, ``cost_series``, ``settlements``,
        ``joule_economy``, and ``errors`` (empty on the happy path).
    """
    errors: list[str] = []
    today = datetime.now(timezone.utc).date().isoformat()

    autopilot = _autopilot_section(today, errors)
    joule_economy = _joule_section(home, errors)

    return {
        "autopilot_cost": autopilot["summary"],
        "cost_series": autopilot["cost_series"],
        "settlements": autopilot["settlements"],
        "joule_economy": joule_economy,
        "errors": errors,
    }
