"""Publish Atlas's operator brief as a static artifact each tick (R1.7).

`skoperator run` calls `publish_brief` with the pass result, writing a
self-contained `index.html` (the atlas host serves it per the site standard) plus
a `brief.md`. This closes the honesty gap that atlas.skworld.io was not backed by
anything in the tree: the brief is now generated in-repo from the real operator
pass, not hand-authored deploy-side.

Pure rendering (no I/O) in `render_html` / `render_markdown`; `publish_brief` is
the one function that touches disk, atomically.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from ..atomic_io import atomic_write_text


def _rows(entries: list[dict[str, Any]]) -> str:
    """Render firing/stale entries as HTML table rows (escaped)."""
    out = []
    for e in entries:
        app = html.escape(str(e.get("app", "")))
        ctype = html.escape(str(e.get("type", "")))
        obj = html.escape(str(e.get("object", "")))
        status = html.escape(str(e.get("status", "")))
        out.append(f"<tr><td>{app}</td><td>{ctype}</td><td>{obj}</td><td>{status}</td></tr>")
    return "\n".join(out)


def render_html(result: dict[str, Any], now_iso: str) -> str:
    """Render one operator pass as a self-contained HTML brief.

    Args:
        result: The dict returned by `loop.run_once`.
        now_iso: The tick timestamp.

    Returns:
        A complete, dependency-free HTML document.
    """
    frozen = bool(result.get("frozen"))
    brief = result.get("brief") or {}
    counts = brief.get("counts") or {}
    firing = brief.get("firing") or []
    stale = brief.get("stale") or []
    outcomes = result.get("outcomes") or []
    route = html.escape(str(result.get("route") or "-"))
    report = html.escape(str(result.get("report") or ""))

    if frozen:
        state = '<span class="frozen">FROZEN</span> Atlas is standing down.'
    elif brief.get("quiet"):
        state = '<span class="ok">ALL QUIET</span> nothing firing.'
    else:
        state = f'<span class="alert">{len(firing)} firing</span>, {len(stale)} stale.'

    firing_table = (
        f"<table><thead><tr><th>app</th><th>condition</th><th>object</th>"
        f"<th>status</th></tr></thead><tbody>{_rows(firing)}</tbody></table>"
        if firing
        else "<p class=muted>none firing</p>"
    )
    stale_table = (
        f"<table><thead><tr><th>app</th><th>condition</th><th>object</th>"
        f"<th>status</th></tr></thead><tbody>{_rows(stale)}</tbody></table>"
        if stale
        else "<p class=muted>none stale</p>"
    )
    disp_rows = "\n".join(
        f"<tr><td>{html.escape(str(o.get('action')))}</td>"
        f"<td>{html.escape(str(o.get('disposition')))}</td>"
        f"<td>{html.escape(str(o.get('outcome')))}</td></tr>"
        for o in outcomes
    )
    disp_table = (
        f"<table><thead><tr><th>action</th><th>disposition</th><th>outcome</th>"
        f"</tr></thead><tbody>{disp_rows}</tbody></table>"
        if outcomes
        else "<p class=muted>no proposals this tick</p>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atlas operator brief</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 900px;
         padding: 2rem 1.25rem; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  .ts {{ color: #888; font-size: .85rem; }}
  .state {{ margin: 1rem 0; font-size: 1.05rem; }}
  .ok {{ color: #16a34a; font-weight: 600; }}
  .alert {{ color: #d97706; font-weight: 600; }}
  .frozen {{ color: #dc2626; font-weight: 700; }}
  .muted {{ color: #999; }}
  table {{ border-collapse: collapse; width: 100%; margin: .5rem 0 1.5rem; }}
  th, td {{ text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #8883; }}
  th {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .03em; color: #888; }}
  pre {{ background: #8881; padding: 1rem; border-radius: 8px; overflow-x: auto;
        white-space: pre-wrap; }}
  h2 {{ font-size: 1rem; margin: 1.5rem 0 .3rem; }}
</style></head><body>
<h1>Atlas operator brief</h1>
<div class="ts">tick {html.escape(now_iso)} &middot; brain route: {route}</div>
<div class="state">{state}</div>
<div class="ts">firing {int(counts.get('firing', len(firing)))} &middot;
 stale {int(counts.get('stale', len(stale)))}</div>
<h2>Firing</h2>{firing_table}
<h2>Stale</h2>{stale_table}
<h2>Dispositions</h2>{disp_table}
<h2>Report</h2><pre>{report}</pre>
</body></html>
"""


def render_markdown(result: dict[str, Any], now_iso: str) -> str:
    """Render one operator pass as a Markdown brief."""
    frozen = bool(result.get("frozen"))
    brief = result.get("brief") or {}
    firing = brief.get("firing") or []
    stale = brief.get("stale") or []
    outcomes = result.get("outcomes") or []

    lines = [
        "# Atlas operator brief",
        "",
        f"tick `{now_iso}` | route `{result.get('route') or '-'}`",
        "",
    ]
    if frozen:
        lines.append("**FROZEN** - Atlas is standing down.")
    elif brief.get("quiet"):
        lines.append("**All quiet** - nothing firing.")
    else:
        lines.append(f"**{len(firing)} firing**, {len(stale)} stale.")
    lines.append("")

    lines.append("## Firing")
    if firing:
        for e in firing:
            obj = f" ({e['object']})" if e.get("object") else ""
            lines.append(f"- `{e.get('app')}` {e.get('type')}{obj} = {e.get('status')}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Dispositions")
    if outcomes:
        for o in outcomes:
            lines.append(f"- {o.get('action')}: {o.get('disposition')} -> {o.get('outcome')}")
    else:
        lines.append("- no proposals this tick")
    lines.append("")

    lines.append("## Report")
    lines.append("```")
    lines.append(str(result.get("report") or ""))
    lines.append("```")
    return "\n".join(lines) + "\n"


def publish_brief(result: dict[str, Any], now_iso: str, out_dir: str | Path) -> dict[str, Path]:
    """Write the brief artifacts (index.html + brief.md) into out_dir, atomically.

    Returns a dict mapping "html"/"markdown" to the written paths.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / "index.html"
    md_path = out / "brief.md"
    atomic_write_text(html_path, render_html(result, now_iso))
    atomic_write_text(md_path, render_markdown(result, now_iso))
    return {"html": html_path, "markdown": md_path}


__all__ = ["render_html", "render_markdown", "publish_brief"]
