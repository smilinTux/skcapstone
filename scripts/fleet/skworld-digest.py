#!/usr/bin/env python3
"""SKWorld fleet digest: collect structured state and render an operations page.

Replaces a bash script that shelled out, captured raw text, and pasted it into
<pre> blocks. That version had three problems this one fixes:

  It could not be read at a glance. Every section was an undifferentiated text
  dump, so "is anything wrong" required reading all of it.

  It surveyed chiap01-03 only, silently omitting chiap08 and chiap04, so every
  fleet total was short by two hosts including the coordination hub.

  Its inline renderer threw re.error every cycle on a regex mangled by shell
  quoting, and wrote nothing.

Collection is defensive throughout. A host that is down, a gateway that does not
answer, a database that is locked: each degrades to a recorded "unavailable" for
that one panel. A digest that dies because one host is rebooting is worse than no
digest, because it looks like a fleet outage.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter

HOSTS = ["chiap01", "chiap02", "chiap03", "chiap08", "chiap04"]
GATEWAY = "http://chiap01:18790"
METRICS_HOST = "chiap01"
METRICS_DB = "~/skgateway-codex/data/metrics.db"
CARDS = os.path.expanduser("~/.skcapstone/cards")
REFRESH_SECONDS = 600


def _run(cmd, timeout=12):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _ssh(host, script, timeout=14):
    return _run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(4, timeout // 3)}",
         f"skuser01@{host}", script],
        timeout=timeout,
    )


def _ssh_python(host, program, timeout=18):
    """Run a Python program on a remote host by feeding it over stdin.

    Not `ssh host python3 -c '...'`: that puts the program through two levels of
    shell quoting, which is how the previous digest ended up with a regex bash
    had mangled into `re.error: unbalanced parenthesis`. stdin has no quoting.

    Python rather than the sqlite3 CLI because chiap01 does not have the sqlite3
    binary installed, while the module is always present. The CLI version failed
    silently and the digest reported zero traffic as though that were healthy.
    """
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(4, timeout // 3)}",
             f"skuser01@{host}", "python3", "-"],
            input=program, capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #

def collect_gateway_queue():
    """Live pool occupancy straight from the gateway, no reporting layer between."""
    raw = _run(["curl", "-fsS", "--max-time", "8", f"{GATEWAY}/queue"], timeout=10)
    if not raw:
        return {"available": False}
    try:
        d = json.loads(raw)
    except ValueError:
        return {"available": False}
    pool = d.get("pool", {}) or {}
    backends = []
    for name, b in (d.get("backends", {}) or {}).items():
        if not b.get("totalProcessed") and not b.get("active"):
            continue
        backends.append({
            "name": name,
            "active": b.get("active", 0),
            "queued": b.get("queued", 0),
            "max": b.get("max", 0),
            "processed": b.get("totalProcessed", 0),
            "dropped": b.get("totalDropped", 0),
            "timedout": b.get("totalTimedOut", 0),
            "peak": b.get("peakActive", 0),
        })
    backends.sort(key=lambda x: -x["processed"])
    return {
        "available": True,
        "active": pool.get("totalActive", 0),
        "queued": pool.get("totalQueued", 0),
        "capacity": pool.get("totalCapacity", 0),
        "backends": backends,
    }


def collect_gateway_lanes():
    """Requests per lane from the gateway's own metrics store.

    started_at is epoch MILLISECONDS and id is random hex, not sequential.
    Filtering the first as text, or ordering by the second, both silently return
    the wrong rows. Both mistakes were made while investigating a reported
    outage that turned out to be a reporting bug.

    Returns an "available" flag. A collection failure must NOT look like zero
    traffic: reporting 0 requests as though it were a healthy measurement is the
    exact thing that made a working gateway look dead.
    """
    prog = """
import json, sqlite3, os, time
db = os.path.expanduser("%s")
now = int(time.time() * 1000)
out = {}
try:
    c = sqlite3.connect("file:%%s?mode=ro" %% db, uri=True)
    for label, mins in (("m10", 10), ("m60", 60)):
        rows = list(c.execute(
            "select backend, model, count(*), "
            "sum(case when status_code >= 400 then 1 else 0 end), "
            "avg(total_ms) from request_log where started_at > ? "
            "group by backend, model", (now - mins * 60 * 1000,)))
        out[label] = [[str(r[0]), str(r[1]), r[2], r[3] or 0, r[4]] for r in rows]
    print(json.dumps({"ok": True, "data": out}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": "%%s: %%s" %% (type(exc).__name__, exc)}))
""" % METRICS_DB
    raw = _ssh_python(METRICS_HOST, prog, timeout=20)
    if not raw:
        return {"available": False, "error": "no response from %s" % METRICS_HOST}
    try:
        res = json.loads(raw)
    except ValueError:
        return {"available": False, "error": "unparseable response"}
    if not res.get("ok"):
        return {"available": False, "error": res.get("error", "unknown")}
    out = {"available": True}
    for label, rows in res["data"].items():
        lanes = {}
        for backend, model, n, errs, ms in rows:
            key = ("codex" if backend == "codex" or model.startswith("sk-codex")
                   else "glm/zai" if backend == "zai" or "glm" in model
                   else backend or "other")
            cur = lanes.setdefault(key, {"n": 0, "errors": 0, "ms": 0.0, "w": 0})
            cur["n"] += n
            cur["errors"] += errs
            if ms:
                cur["ms"] += ms * n
                cur["w"] += n
        for v in lanes.values():
            v["avg_ms"] = int(v["ms"] / v["w"]) if v["w"] else None
        out[label] = lanes
    return out


def collect_host(host):
    """One host: workers by lane, timer health, and its view of the pool."""
    script = (
        "printf '%s\\t' \"$(tmux ls 2>/dev/null | grep -c '^codex-auto-')\"; "
        "printf '%s\\t' \"$(tmux ls 2>/dev/null | grep -c '^glm-auto-')\"; "
        "printf '%s\\t' \"$(systemctl --user is-active skfleet-rotate.timer 2>/dev/null)\"; "
        "printf '%s\\t' \"$(journalctl --user -u skfleet-rotate.service --no-pager -o cat -n 30 2>/dev/null "
        "| grep -oE 'ready=[0-9]+' | tail -1 | cut -d= -f2)\"; "
        "printf '%s\\t' \"$(journalctl --user -u skfleet-rotate.service --no-pager -o cat -n 30 2>/dev/null "
        "| grep -oE 'blocked_backoff=[0-9]+' | tail -1 | cut -d= -f2)\"; "
        "printf '%s\\t' \"$(journalctl --user -u skfleet-rotate.service --no-pager -o cat --since '-20 min' 2>/dev/null "
        "| grep -c 'LAUNCHED|')\"; "
        "printf '%s' \"$(journalctl --user -u skfleet-rotate.service --no-pager -o cat --since '-20 min' 2>/dev/null "
        "| grep -c 'BLOCKED|.*lifecycle')\""
    )
    raw = _ssh(host, script, timeout=18)
    if not raw:
        return {"host": host, "up": False}
    f = (raw.split("\t") + [""] * 7)[:7]

    def num(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    return {
        "host": host, "up": True,
        "codex": num(f[0]) or 0, "glm": num(f[1]) or 0,
        "timer": f[2] or "unknown",
        "ready": num(f[3]), "backoff": num(f[4]),
        "launched20m": num(f[5]) or 0, "aborts": num(f[6]) or 0,
    }


def collect_board():
    """Card throughput and composition, read straight from the CardStore."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cuts = {"m10": now - datetime.timedelta(minutes=10),
            "m60": now - datetime.timedelta(minutes=60)}
    thr = {k: Counter() for k in cuts}
    states = Counter()
    lc = {"claim": "claimed", "release_claim": "open", "unassign": "open",
          "archive": "void", "complete": "complete", "void": "void"}
    try:
        card_ids = os.listdir(CARDS)
    except OSError:
        return {"available": False}
    for cid in card_ids:
        ev = os.path.join(CARDS, cid, "events")
        if not os.path.isdir(ev):
            continue
        rows = []
        try:
            for fn in os.listdir(ev):
                with open(os.path.join(ev, fn), encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            parsed = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(parsed, dict):
                            rows.append(parsed)
        except OSError:
            continue
        rows.sort(key=lambda e: (e.get("ts", ""), str(e.get("writer", ""))))
        st, col = "open", None
        for e in rows:
            a = e.get("action")
            ts = e.get("ts", "")
            if a in ("claim", "complete", "release_claim") and ts:
                for k, cut in cuts.items():
                    if ts > cut.isoformat():
                        thr[k][a] += 1
            if a == "move":
                c = str(e.get("column") or "").strip().lower()
                if c:
                    col = c
                continue
            if a in lc:
                if st in ("complete", "void"):
                    continue
                st = lc[a]
        if st not in ("complete", "void") and col == "done":
            st = "complete"
        states[st] += 1
    return {"available": True, "throughput": {k: dict(v) for k, v in thr.items()},
            "states": dict(states)}


# --------------------------------------------------------------------------- #
# assessment
# --------------------------------------------------------------------------- #

def assess(gw, lanes, hosts, board):
    """Turn the numbers into things a person should act on."""
    alerts = []
    if not gw.get("available"):
        alerts.append(("critical", "Gateway /queue did not answer",
                       "No model traffic can be served. Check skgateway-codex on chiap01."))
    down = [h["host"] for h in hosts if not h.get("up")]
    if down:
        alerts.append(("warn", f"{len(down)} host(s) unreachable: {', '.join(down)}",
                       "A scheduled reboot looks identical to a failure here. Confirm which."))
    for h in hosts:
        if h.get("up") and h.get("timer") not in ("active", None, "unknown"):
            alerts.append(("critical", f"{h['host']} rotation timer is {h['timer']}",
                           "That host launches no workers until the timer is active."))
        if h.get("up") and (h.get("aborts") or 0) > 0:
            alerts.append(("warn", f"{h['host']} logged {h['aborts']} lifecycle abort(s) in 20 min",
                           "The rotation exits before launching when the assessment fails."))
    errs = sum(v.get("errors", 0) for v in (lanes.get("m60") or {}).values())
    tot = sum(v.get("n", 0) for v in (lanes.get("m60") or {}).values())
    if not lanes.get("available"):
        alerts.append(("critical", "Gateway metrics could not be read",
                       "This panel shows zero, which is NOT a measurement of zero traffic. "
                       + str(lanes.get("error", ""))))
    elif tot == 0 and gw.get("available") and gw.get("active", 0) > 0:
        alerts.append(("warn", "Gateway is serving requests but the metrics store reports none",
                       "The pool shows active work. Suspect the metrics collector, not the gateway."))
    if tot and errs / tot > 0.05:
        alerts.append(("warn", f"{errs} gateway errors in the last hour ({100.0*errs/tot:.0f}%)",
                       "Check credentials and upstream provider health."))
    workers = sum((h.get("codex", 0) + h.get("glm", 0)) for h in hosts if h.get("up"))
    ready = sum(h.get("ready") or 0 for h in hosts if h.get("up"))
    if workers == 0 and ready > 0:
        alerts.append(("warn", f"No workers running while {ready} card(s) read as ready",
                       "Cards may be owned by a host that is down, or excluded after the pool count."))
    if not alerts:
        alerts.append(("ok", "Nothing needs attention", "All checks passed on this pass."))
    return alerts


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0E141B;color:#E6EDF3;
 font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:26px 22px 70px}
a{color:#38BDF8}
h1{font-size:21px;margin:0;font-weight:600;letter-spacing:-.01em}
h2{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#7D8896;
 margin:34px 0 11px;font-weight:600}
.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-variant-numeric:tabular-nums}
.head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
 border-bottom:1px solid #1E2733;padding-bottom:14px}
.head .sub{color:#7D8896;font-size:12.5px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px}
.ok{background:#4ADE80}.warn{background:#FBBF24}.critical{background:#F87171}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:11px;margin:18px 0 0}
.stat{background:#161E27;border:1px solid #1E2733;border-radius:7px;padding:13px 15px}
.stat .n{font-size:25px;font-weight:600;line-height:1.1}
.stat .l{font-size:11.5px;color:#7D8896;margin-top:4px}
.stat.good .n{color:#4ADE80}.stat.bad .n{color:#F87171}.stat.warn .n{color:#FBBF24}
.stat.info .n{color:#38BDF8}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
 color:#7D8896;padding:7px 9px;border-bottom:1px solid #1E2733;font-weight:600}
td{padding:8px 9px;border-bottom:1px solid #161E27}
tr:last-child td{border-bottom:0}
.panel{background:#161E27;border:1px solid #1E2733;border-radius:7px;overflow:hidden}
.bar{height:6px;border-radius:3px;background:#1E2733;overflow:hidden;min-width:80px}
.bar>span{display:block;height:100%}
.alert{display:flex;gap:11px;padding:11px 14px;border-bottom:1px solid #161E27;align-items:flex-start}
.alert:last-child{border-bottom:0}
.alert .t{font-weight:600}
.alert .d{color:#7D8896;font-size:12.5px;margin-top:2px}
.muted{color:#7D8896}
.right{text-align:right}
.scroll{overflow-x:auto}
footer{margin-top:38px;padding-top:15px;border-top:1px solid #1E2733;
 color:#5C6672;font-size:11.5px}
"""


def esc(x):
    return html.escape(str(x))


def bar(pct, colour):
    pct = max(0.0, min(100.0, pct))
    return f'<div class="bar"><span style="width:{pct:.1f}%;background:{colour}"></span></div>'


def render(gw, lanes, hosts, board, alerts, generated):
    worst = "ok"
    for level, _, _ in alerts:
        if level == "critical":
            worst = "critical"
            break
        if level == "warn":
            worst = "warn"
    label = {"ok": "Healthy", "warn": "Degraded", "critical": "Attention needed"}[worst]

    up = [h for h in hosts if h.get("up")]
    workers = sum(h.get("codex", 0) + h.get("glm", 0) for h in up)
    ready = sum(h.get("ready") or 0 for h in up)
    l60 = lanes.get("m60") or {}
    req60 = sum(v.get("n", 0) for v in l60.values())
    err60 = sum(v.get("errors", 0) for v in l60.values())
    thr = board.get("throughput", {}) if board.get("available") else {}
    done60 = thr.get("m60", {}).get("complete", 0)
    claim60 = thr.get("m60", {}).get("claim", 0)

    p = []
    p.append(f"<!doctype html><html><head><meta charset='utf-8'>")
    p.append(f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>")
    p.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    p.append("<title>SKWorld Fleet</title>")
    p.append(f"<style>{CSS}</style></head><body><div class='wrap'>")

    p.append("<div class='head'>")
    p.append(f"<h1><span class='dot {worst}'></span>SKWorld Fleet &middot; {label}</h1>")
    p.append(f"<span class='sub mono'>{esc(generated)}</span>")
    p.append(f"<span class='sub'>refreshes every {REFRESH_SECONDS // 60} min</span>")
    p.append("</div>")

    # headline strip
    p.append("<div class='strip'>")
    for cls, n, l in (
        ("good" if workers else "warn", workers, "workers running"),
        ("info", ready, "cards ready"),
        ("good" if done60 else "", done60, "completed, 60 min"),
        ("", claim60, "claimed, 60 min"),
        ("info", f"{req60:,}", "gateway requests, 60 min"),
        ("bad" if err60 else "good", err60, "gateway errors, 60 min"),
        ("good" if len(up) == len(hosts) else "warn", f"{len(up)}/{len(hosts)}", "hosts up"),
    ):
        p.append(f"<div class='stat {cls}'><div class='n mono'>{esc(n)}</div><div class='l'>{esc(l)}</div></div>")
    p.append("</div>")

    # alerts
    p.append("<h2>Needs attention</h2><div class='panel'>")
    for level, title, detail in alerts:
        p.append(f"<div class='alert'><span class='dot {level}' style='margin-top:6px'></span>"
                 f"<div><div class='t'>{esc(title)}</div><div class='d'>{esc(detail)}</div></div></div>")
    p.append("</div>")

    # gateway
    p.append("<h2>Gateway</h2><div class='panel scroll'><table>")
    p.append("<tr><th>lane</th><th class='right'>10 min</th><th class='right'>60 min</th>"
             "<th class='right'>errors</th><th class='right'>avg</th><th style='width:200px'>share, 60 min</th></tr>")
    if not lanes.get("available"):
        p.append("<tr><td colspan='6' style='color:#F87171'>Metrics unreadable: "
                 + esc(lanes.get("error", "unknown")) + ". This is NOT zero traffic.</td></tr>")
    elif req60 or lanes.get("m10"):
        for name in sorted(set(list(l60) + list(lanes.get("m10") or {})),
                           key=lambda k: -l60.get(k, {}).get("n", 0)):
            a = (lanes.get("m10") or {}).get(name, {})
            b = l60.get(name, {})
            n60 = b.get("n", 0)
            share = 100.0 * n60 / req60 if req60 else 0
            e = b.get("errors", 0)
            avg = b.get("avg_ms")
            colour = "#F87171" if e else "#38BDF8"
            p.append(f"<tr><td class='mono'>{esc(name)}</td>"
                     f"<td class='right mono'>{a.get('n',0):,}</td>"
                     f"<td class='right mono'>{n60:,}</td>"
                     f"<td class='right mono' style='color:{'#F87171' if e else '#5C6672'}'>{e}</td>"
                     f"<td class='right mono muted'>{(str(avg)+'ms') if avg else '-'}</td>"
                     f"<td>{bar(share, colour)}</td></tr>")
    else:
        p.append("<tr><td colspan='6' class='muted'>No gateway metrics in this window. "
                 "A lane absent here sent nothing; it is not evidence of a fault.</td></tr>")
    p.append("</table></div>")

    if gw.get("available"):
        p.append("<div class='panel scroll' style='margin-top:11px'><table>")
        p.append("<tr><th>backend</th><th class='right'>active</th><th class='right'>queued</th>"
                 "<th class='right'>max</th><th class='right'>peak</th><th class='right'>processed</th>"
                 "<th class='right'>dropped</th><th class='right'>timed out</th></tr>")
        for b in gw["backends"]:
            bad = b["dropped"] or b["timedout"]
            p.append(f"<tr><td class='mono'>{esc(b['name'])}</td>"
                     f"<td class='right mono'>{b['active']}</td><td class='right mono'>{b['queued']}</td>"
                     f"<td class='right mono muted'>{b['max']}</td><td class='right mono muted'>{b['peak']}</td>"
                     f"<td class='right mono'>{b['processed']:,}</td>"
                     f"<td class='right mono' style='color:{'#F87171' if b['dropped'] else '#5C6672'}'>{b['dropped']}</td>"
                     f"<td class='right mono' style='color:{'#F87171' if b['timedout'] else '#5C6672'}'>{b['timedout']}</td></tr>")
        p.append("</table></div>")
    else:
        p.append("<div class='panel' style='margin-top:11px;padding:13px 15px' class='muted'>"
                 "Gateway /queue unavailable.</div>")

    # fleet
    p.append("<h2>Fleet</h2><div class='panel scroll'><table>")
    p.append("<tr><th>host</th><th>timer</th><th class='right'>codex</th><th class='right'>glm</th>"
             "<th class='right'>ready</th><th class='right'>backoff</th>"
             "<th class='right'>launched 20m</th><th class='right'>aborts</th></tr>")
    for h in hosts:
        if not h.get("up"):
            p.append(f"<tr><td class='mono'>{esc(h['host'])}</td>"
                     f"<td colspan='7' class='muted'>unreachable on this pass</td></tr>")
            continue
        t = h.get("timer", "?")
        tcol = "#4ADE80" if t == "active" else "#F87171"
        ab = h.get("aborts", 0)
        p.append(f"<tr><td class='mono'>{esc(h['host'])}</td>"
                 f"<td class='mono' style='color:{tcol}'>{esc(t)}</td>"
                 f"<td class='right mono'>{h.get('codex',0)}</td>"
                 f"<td class='right mono'>{h.get('glm',0)}</td>"
                 f"<td class='right mono'>{h.get('ready') if h.get('ready') is not None else '-'}</td>"
                 f"<td class='right mono muted'>{h.get('backoff') if h.get('backoff') is not None else '-'}</td>"
                 f"<td class='right mono'>{h.get('launched20m',0)}</td>"
                 f"<td class='right mono' style='color:{'#F87171' if ab else '#5C6672'}'>{ab}</td></tr>")
    p.append("</table></div>")

    # board
    p.append("<h2>Board</h2>")
    if board.get("available"):
        st = board["states"]
        total = sum(st.values()) or 1
        p.append("<div class='panel scroll'><table>")
        p.append("<tr><th>state</th><th class='right'>cards</th><th style='width:280px'>share</th></tr>")
        colours = {"complete": "#4ADE80", "open": "#38BDF8",
                   "claimed": "#FBBF24", "void": "#5C6672"}
        for k, v in sorted(st.items(), key=lambda x: -x[1]):
            p.append(f"<tr><td class='mono'>{esc(k)}</td><td class='right mono'>{v:,}</td>"
                     f"<td>{bar(100.0*v/total, colours.get(k,'#7D8896'))}</td></tr>")
        p.append("</table></div>")
        t10 = thr.get("m10", {})
        p.append("<div class='strip' style='margin-top:11px'>")
        for lab, key in (("claimed", "claim"), ("completed", "complete"), ("released", "release_claim")):
            p.append(f"<div class='stat'><div class='n mono'>{t10.get(key,0)}</div>"
                     f"<div class='l'>{lab}, last 10 min</div></div>")
        p.append("</div>")
    else:
        p.append("<div class='panel' style='padding:13px 15px'><span class='muted'>"
                 "CardStore unreadable on this pass.</span></div>")

    p.append("<footer>Read-only snapshot. Collected per pass; a panel marked unavailable "
             "failed on this pass only and does not imply a fault. "
             f"Sources: {esc(GATEWAY)}/queue, request_log on {esc(METRICS_HOST)}, "
             "per-host systemd and tmux, and the CardStore.</footer>")
    p.append("</div></body></html>")
    return "".join(p)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/skworld-fleet-digest.html"
    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        f_gw = ex.submit(collect_gateway_queue)
        f_lanes = ex.submit(collect_gateway_lanes)
        f_board = ex.submit(collect_board)
        f_hosts = [ex.submit(collect_host, h) for h in HOSTS]
        gw = f_gw.result()
        lanes = f_lanes.result()
        board = f_board.result()
        hosts = [f.result() for f in f_hosts]
    alerts = assess(gw, lanes, hosts, board)
    page = render(gw, lanes, hosts, board, alerts, generated)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(tmp, target)   # atomic: a reader never sees a half-written page
    print("%s  %d bytes  %d alert(s)" % (target, len(page), len(alerts)))


if __name__ == "__main__":
    main()
